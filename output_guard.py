"""Output guard for AUTO replies (audit T1-4, full).

Pure-Python check run on the model's reply before an Instagram AUTO reply
is sent. It never rewrites text: a reply that trips any rule is held back
and routed to DRAFT+APPROVE by the caller (app._ig_output_guard), so the
founder sees exactly what the model wrote.

Rules (each returns a short reason naming the rule and what matched):
  1. A ₹ / Rs / INR amount that isn't allowed. The caller passes the live
     Shopify prices; FIXED_ALLOWED_INR adds brain.md's fixed amounts. An
     order total also passes when it is a sum of up to MAX_ORDER_ITEMS
     live prices (repeats allowed) and at most the Hard Money Threshold
     (₹1,500 for Glam Shelf) — larger totals still go to a draft.
  2. code, coupon, promo, discount, refund, cashback or "free" — except
     the approved phrases in _APPROVED_PHRASES ("free shipping",
     "shipping is free", "cruelty-free", the discount decline).
  3. A link, domain, email or @handle other than the brand's own website
     domain, instagram.com/<handle>, @handle and email, plus any extras
     in the brand file's allowed_links (Glam Shelf: glamshelf.in,
     instagram.com/glamshelfstore, @glamshelfstore and the brand email).
  4. Prompt-injection phrasing: "system prompt", "my/your instructions",
     "ignore previous", QA mode or classifying. Plain "instructions"
     ("care instructions") and "brain" in product text pass.

Every brand value (amounts, domain, handle, email) comes from the brand
settings file (brand_config; brands/glamshelf.json by default). check_reply
takes an optional `brand` dict so another brand's rules can be checked
without re-importing.

Rollback: OUTPUT_GUARD_DISABLED=1 (see app._ig_output_guard).
"""
from __future__ import annotations

import re
from typing import NamedTuple
from urllib.parse import urlsplit

from brand_config import BRAND
from pricing_rules import HARD_MONEY_THRESHOLD_INR


def _fixed_amounts(pricing: dict) -> frozenset:
    """brain.md's fixed amounts, allowed on top of the live Shopify prices.
    Glam Shelf's values are in the comments."""
    return frozenset({
        pricing["free_shipping_threshold_inr"],  # Section 2: free shipping above ₹799
        pricing["hard_money_threshold_inr"],     # ₹1,500 Hard Money Threshold
        pricing["bulk_rate_inr"],                # ₹749/tray bulk rate (Rule 3b)
        pricing["bulk_floor_inr"],               # Section 2: ₹699 absolute bulk floor
        *pricing["extra_allowed_inr"],           # ~₹85 per pair (cost-per-wear reframe),
                                                 # ~₹12–17 per wear, "cheap ₹50 white glues"
    })


FREE_SHIPPING_THRESHOLD_INR = BRAND["pricing"]["free_shipping_threshold_inr"]
BULK_FLOOR_INR = BRAND["pricing"]["bulk_floor_inr"]
FIXED_ALLOWED_INR = _fixed_amounts(BRAND["pricing"])

# Rule 1 order totals: "2 GS1 trays" style sums of live prices, up to this
# many items and no higher than the Hard Money Threshold.
MAX_ORDER_ITEMS = 6

BRAND_EMAIL = BRAND["email"]
BRAND_HANDLE = BRAND["instagram_handle"]


class Rules(NamedTuple):
    """One brand's guard settings, derived from its brand file."""
    fixed_amounts: frozenset
    total_cap_inr: float
    domains: frozenset
    handles: frozenset
    emails: frozenset


def rules_for(brand: dict) -> Rules:
    """The brand's own domain / handle / email are always allowed, plus
    the extras listed under allowed_links."""
    links = brand["allowed_links"]
    return Rules(
        fixed_amounts=_fixed_amounts(brand["pricing"]),
        total_cap_inr=brand["pricing"]["hard_money_threshold_inr"],
        domains=frozenset({brand["website_domain"], *links["domains"]}),
        handles=frozenset({brand["instagram_handle"], *links["instagram_handles"]}),
        emails=frozenset({brand["email"], *links["emails"]}),
    )


_BRAND_RULES = rules_for(BRAND)

# ₹849 / ₹ 1,500 / Rs. 849 / INR 849 / ₹849.00
_AMOUNT_RE = re.compile(r"(?:₹|\bRs\.?|\bINR)\s?(\d[\d,]*\d|\d)(\.\d{1,2})?", re.IGNORECASE)

# Rule 2. Approved phrases are removed before the word scan, so they pass
# while any other use of the same word still fires.
_APPROVED_PHRASES = re.compile(
    r"free shipping|shipping is (?:also )?free|ships (?:for )?free|cruelty[- ]free"
    r"|no (?:additional|extra|other) discounts?(?: (?:available|at the moment|right now))?"
    r"|no active discount codes?",
    re.IGNORECASE,
)
_PROMO_WORDS_RE = re.compile(
    r"\b(codes?|coupons?|promos?|promo[- ]?codes?|discount\w*|refund\w*|cashbacks?|free\w*)\b",
    re.IGNORECASE,
)

# Rule 3.
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_HANDLE_RE = re.compile(r"(?<![\w.@])@([A-Za-z0-9._]+)")
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+|www\.[^\s<>\"')\]]+", re.IGNORECASE)
# A bare domain: labels, then a lowercase TLD (case-sensitive on purpose,
# so a missing space like "₹849.Free" isn't read as a domain).
_BARE_DOMAIN_RE = re.compile(r"\b(?:[A-Za-z0-9-]+\.)+[a-z]{2,24}\b(?:/[^\s<>\"')\]]*)?")

# Rule 4.
_META_RE = re.compile(
    r"system[- ]?prompt|\b(?:my|your) instructions?\b|\bignore (?:all |any |the )?previous\b"
    r"|\bqa[- ]?mode\b|\bclassif(?:y|ied|ies|ying|ication)\b",
    re.IGNORECASE,
)


def _amount_value(digits: str, fraction: str | None) -> float:
    value = float(digits.replace(",", ""))
    if fraction:
        value += float(fraction)
    return value


def _order_totals_paise(prices, cap_inr: float = HARD_MONEY_THRESHOLD_INR) -> set[int]:
    """Every sum of 1..MAX_ORDER_ITEMS live prices (repeats allowed) that is
    at most `cap_inr` (the Hard Money Threshold), in paise so floats compare
    exactly. Built from the live prices only — never FIXED_ALLOWED_INR."""
    cap = cap_inr * 100
    units = {round(float(p) * 100) for p in prices or ()}
    units = {u for u in units if 0 < u <= cap}
    totals: set[int] = set()
    layer = {0}
    for _ in range(MAX_ORDER_ITEMS):
        layer = {t + u for t in layer for u in units if t + u <= cap}
        if not layer:
            break
        totals |= layer
    return totals


def _link_allowed(link: str, rules: Rules = _BRAND_RULES) -> bool:
    link = link.rstrip(".,!?;:")
    parts = urlsplit(link if "://" in link else "https://" + link)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host in rules.domains:
        return True
    if host == "instagram.com":
        first = parts.path.strip("/").split("/")[0].lower()
        return first in rules.handles
    return False


def check_reply(text: str, allowed_amounts, brand: dict | None = None) -> list[str]:
    """Return one reason per rule that fired ([] = the reply may be sent).
    `allowed_amounts` are the live product prices; the brand's fixed
    amounts (FIXED_ALLOWED_INR) are always added, and so are order totals
    up to the Hard Money Threshold built from the live prices
    (_order_totals_paise). `brand` defaults to the loaded brand file."""
    rules = _BRAND_RULES if brand is None else rules_for(brand)
    text = text or ""
    allowed = set(rules.fixed_amounts) | {float(a) for a in allowed_amounts or ()}
    totals = _order_totals_paise(allowed_amounts, rules.total_cap_inr)
    reasons: list[str] = []

    def amount_ok(value: float) -> bool:
        return value in allowed or round(value * 100) in totals

    bad = [m.group(0) for m in _AMOUNT_RE.finditer(text)
           if not amount_ok(_amount_value(m.group(1), m.group(2)))]
    if bad:
        reasons.append(f"rule 1 (₹ amount not allowed): {', '.join(bad)}")

    words = [m.group(0) for m in _PROMO_WORDS_RE.finditer(_APPROVED_PHRASES.sub(" ", text))]
    if words:
        reasons.append(f"rule 2 (code/discount/refund/free words): {', '.join(words)}")

    rest = text
    links = []
    for m in _EMAIL_RE.finditer(text):
        if m.group(0).lower() not in rules.emails:
            links.append(m.group(0))
    rest = _EMAIL_RE.sub(" ", rest)
    for m in _HANDLE_RE.finditer(rest):
        if m.group(1).rstrip(".").lower() not in rules.handles:
            links.append("@" + m.group(1))
    for m in _URL_RE.finditer(rest):
        if not _link_allowed(m.group(0), rules):
            links.append(m.group(0))
    rest = _URL_RE.sub(" ", rest)
    for m in _BARE_DOMAIN_RE.finditer(rest):
        if not _link_allowed(m.group(0), rules):
            links.append(m.group(0))
    if links:
        reasons.append(f"rule 3 (link/domain not allowed): {', '.join(links)}")

    meta = [m.group(0) for m in _META_RE.finditer(text)]
    if meta:
        reasons.append(f"rule 4 (talks about its own setup): {', '.join(meta)}")

    return reasons
