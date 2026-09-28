"""Per-brand settings (multi-brand support).

Twin can run as a separate copy per brand: its own Render service, Meta
app and settings. Everything brand-specific that a customer can see, or
that steers behaviour (links the output guard allows, price rules, store
URLs, canned messages), lives in ONE JSON file per brand:

  - BRAND_CONFIG_PATH unset  -> brands/glamshelf.json, which holds The
    Glam Shelf's values exactly as they were hard-coded before
    (tests/test_brand_golden.py pins every one of them).
  - BRAND_CONFIG_PATH set    -> that file. A relative path is resolved
    against the project folder; on Render use a Secret File, which is
    mounted at /etc/secrets/<filename>.

A brand file is complete on its own. Nothing is merged over Glam Shelf's
values, so a key a new brand forgot can never put glamshelf.in in its
replies. A missing key, a wrong type, an unknown key or an unreadable
file stops the app at startup (same fail-fast rule as app._require_env).
Features that can be switched off take an explicit null. Keys starting
with "_" are comments and ignored.

Pure data: no Flask; the env var and the file are read once, at import.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.resolve()
DEFAULT_BRAND_CONFIG_PATH = PROJECT_DIR / "brands" / "glamshelf.json"


class BrandConfigError(RuntimeError):
    """The brand settings file is missing, unreadable or incomplete."""


# ----- Schema -----
# A spec is: STR (non-empty string), OPT_STR (string or null), NUM
# (positive number), INT (positive whole number), [spec] (list of spec),
# or a dict of key -> spec (every key required, no unknown keys).
STR, OPT_STR, NUM, INT = "str", "str|null", "number", "int"

_PAGES = [{"title": STR, "url": STR}]

SCHEMA = {
    "brand_name": STR,
    "brand_short_name": STR,
    "instagram_handle": STR,
    "email": STR,
    "website_domain": STR,
    "dashboard_api_base": OPT_STR,
    "allowed_links": {
        "domains": [STR],
        "instagram_handles": [STR],
        "emails": [STR],
    },
    "vision": {
        "brand_description": STR,
        "product_singular": STR,
        "product_plural": STR,
        "example_product": STR,
    },
    "shopify": {
        "products_url": OPT_STR,
        "prompt_policy_pages": _PAGES,
        "rag_policy_pages": _PAGES,
    },
    "pricing": {
        "free_shipping_threshold_inr": NUM,
        "hard_money_threshold_inr": NUM,
        "bulk_rate_inr": NUM,
        "bulk_floor_inr": NUM,
        "bulk_min_units": INT,
        "extra_allowed_inr": [NUM],
    },
    "messages": {
        "review_request": OPT_STR,
        "lead_reply": STR,
        "allergy_holding_line": STR,
        "escalate_fallback_holding_reply": STR,
        "instagram_photo_reply": STR,
        "shipped_intro": STR,
        "out_for_delivery": STR,
        "delivered": STR,
    },
    "shipping": {
        "tracking_url_prefix": STR,
        "default_carrier": STR,
        "wati_template_name": STR,
    },
    "bot_text_signatures": [STR],
}

# Placeholders each message template may use. Checked at load, so a typo
# ({firstname}) or a stray brace fails at startup, not mid-send.
_TEMPLATE_FIELDS = {
    "review_request": ("first_name",),
    "shipped_intro": ("first_name", "order_number"),
    "out_for_delivery": ("first_name", "order_number"),
    "delivered": ("first_name", "order_number"),
}


def _check(value, spec, where: str) -> None:
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            raise BrandConfigError(f"{where or 'top level'} must be an object")
        keys = {k for k in value if not k.startswith("_")}
        missing = sorted(set(spec) - keys)
        unknown = sorted(keys - set(spec))
        if missing:
            raise BrandConfigError(f"missing key(s) {', '.join(_join(where, k) for k in missing)}")
        if unknown:
            raise BrandConfigError(f"unknown key(s) {', '.join(_join(where, k) for k in unknown)}")
        for key, sub in spec.items():
            _check(value[key], sub, _join(where, key))
    elif isinstance(spec, list):
        if not isinstance(value, list):
            raise BrandConfigError(f"{where} must be a list")
        for i, item in enumerate(value):
            _check(item, spec[0], f"{where}[{i}]")
    elif spec == OPT_STR:
        if value is not None and not (isinstance(value, str) and value.strip()):
            raise BrandConfigError(f"{where} must be text or null")
    elif spec == STR:
        if not (isinstance(value, str) and value.strip()):
            raise BrandConfigError(f"{where} must be non-empty text")
    elif spec == NUM:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise BrandConfigError(f"{where} must be a positive number")
    elif spec == INT:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise BrandConfigError(f"{where} must be a positive whole number")


def _join(where: str, key: str) -> str:
    return f"{where}.{key}" if where else key


def _check_templates(messages: dict) -> None:
    for key, fields in _TEMPLATE_FIELDS.items():
        template = messages.get(key)
        if template is None:
            continue
        try:
            template.format(**{f: "x" for f in fields})
        except (KeyError, IndexError, ValueError) as e:
            raise BrandConfigError(
                f"messages.{key} has a bad placeholder ({type(e).__name__}: {e}); "
                f"allowed: {', '.join('{' + f + '}' for f in fields)}"
            ) from None


def _normalise(data: dict) -> dict:
    """Lower-case what the output guard compares lower-cased, and drop a
    leading @ / www. a founder might paste."""
    def handle(h: str) -> str:
        return h.strip().lstrip("@").lower()

    def domain(d: str) -> str:
        d = d.strip().lower()
        return d[4:] if d.startswith("www.") else d

    data["instagram_handle"] = handle(data["instagram_handle"])
    data["email"] = data["email"].strip().lower()
    data["website_domain"] = domain(data["website_domain"])
    links = data["allowed_links"]
    links["domains"] = [domain(d) for d in links["domains"]]
    links["instagram_handles"] = [handle(h) for h in links["instagram_handles"]]
    links["emails"] = [e.strip().lower() for e in links["emails"]]
    # Matched against lower-cased, whitespace-collapsed message text.
    data["bot_text_signatures"] = [" ".join(s.split()).lower() for s in data["bot_text_signatures"]]
    return data


def resolve_path(raw: str | os.PathLike | None, default: Path) -> Path:
    """An env-var path: empty -> default; relative -> under the project folder."""
    text = str(raw or "").strip()
    if not text:
        return default
    path = Path(text)
    return path if path.is_absolute() else PROJECT_DIR / path


def load_brand_config(path: str | os.PathLike | None = None) -> dict:
    """Read, validate and normalise one brand file. `path` defaults to
    BRAND_CONFIG_PATH, then brands/glamshelf.json. Raises BrandConfigError."""
    if path is None:
        path = os.environ.get("BRAND_CONFIG_PATH")
    file = resolve_path(path, DEFAULT_BRAND_CONFIG_PATH)
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BrandConfigError(f"brand settings file not found: {file}") from None
    except (OSError, ValueError) as e:
        raise BrandConfigError(f"brand settings file {file} unreadable: {type(e).__name__}: {e}") from None
    try:
        _check(data, SCHEMA, "")
        _check_templates(data["messages"])
    except BrandConfigError as e:
        raise BrandConfigError(f"brand settings file {file}: {e}") from None
    for sig in data["bot_text_signatures"]:
        if len(sig.strip()) < 8:
            # A short signature would match a human's own reply and
            # mis-attribute it to the bot (see app._looks_like_bot_text).
            raise BrandConfigError(
                f"brand settings file {file}: bot_text_signatures entry {sig!r} "
                "is too short (8+ characters)"
            )
    data = _normalise(data)
    data["_file"] = str(file)
    return data


BRAND = load_brand_config()
BRAND_CONFIG_FILE = Path(BRAND["_file"])
BRAND_IS_DEFAULT = BRAND_CONFIG_FILE.resolve() == DEFAULT_BRAND_CONFIG_PATH.resolve()
