"""Output guard for AUTO replies (audit T1-4, full).

Pure-Python check run on the model's reply before an Instagram AUTO reply
is sent. It never rewrites text: a reply that trips any rule is held back
and routed to DRAFT+APPROVE by the caller (app._ig_output_guard), so the
founder sees exactly what the model wrote.

Rules (each returns a short reason naming the rule and what matched):
  1. A ₹ / Rs / INR amount that isn't allowed. The caller passes the live
     Shopify prices; FIXED_ALLOWED_INR adds brain.md's fixed amounts.
  2. code, coupon, promo, discount, refund, cashback or "free" — except
     the approved phrases in _APPROVED_PHRASES ("free shipping",
     "shipping is free", "cruelty-free", the discount decline).
  3. A link, domain, email or @handle other than glamshelf.in,
     instagram.com/glamshelfstore, @glamshelfstore and the brand email.
  4. Talk of the system prompt, instructions, the brain, QA mode or
     classifying.

Rollback: OUTPUT_GUARD_DISABLED=1 (see app._ig_output_guard).
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from pricing_rules import BULK_RATE_INR, HARD_MONEY_THRESHOLD_INR

# brain.md's fixed amounts, allowed on top of the live Shopify prices.
FREE_SHIPPING_THRESHOLD_INR = 799   # Section 2: free shipping above ₹799
BULK_FLOOR_INR = 699                # Section 2: absolute bulk floor
FIXED_ALLOWED_INR = frozenset({
    FREE_SHIPPING_THRESHOLD_INR,
    HARD_MONEY_THRESHOLD_INR,       # ₹1,500 Hard Money Threshold
    BULK_RATE_INR,                  # ₹749/tray bulk rate (Rule 3b)
    BULK_FLOOR_INR,
    85,                             # ~₹85 per pair (cost-per-wear reframe)
    12, 17,                         # ~₹12–17 per wear
    50,                             # "cheap ₹50 white glues" (lash glue)
})

BRAND_EMAIL = "glamshelfstore@gmail.com"
BRAND_HANDLE = "glamshelfstore"

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
    r"system[- ]?prompt|\binstructions?\b|\bbrain\b|\bqa[- ]?mode\b|\bclassif(?:y|ied|ies|ying|ication)\b",
    re.IGNORECASE,
)


def _amount_value(digits: str, fraction: str | None) -> float:
    value = float(digits.replace(",", ""))
    if fraction:
        value += float(fraction)
    return value


def _link_allowed(link: str) -> bool:
    link = link.rstrip(".,!?;:")
    parts = urlsplit(link if "://" in link else "https://" + link)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host == "glamshelf.in":
        return True
    if host == "instagram.com":
        first = parts.path.strip("/").split("/")[0].lower()
        return first == BRAND_HANDLE
    return False


def check_reply(text: str, allowed_amounts) -> list[str]:
    """Return one reason per rule that fired ([] = the reply may be sent).
    `allowed_amounts` are the live product prices; FIXED_ALLOWED_INR is
    always added."""
    text = text or ""
    allowed = set(FIXED_ALLOWED_INR) | {float(a) for a in allowed_amounts or ()}
    reasons: list[str] = []

    bad = [m.group(0) for m in _AMOUNT_RE.finditer(text)
           if _amount_value(m.group(1), m.group(2)) not in allowed]
    if bad:
        reasons.append(f"rule 1 (₹ amount not allowed): {', '.join(bad)}")

    words = [m.group(0) for m in _PROMO_WORDS_RE.finditer(_APPROVED_PHRASES.sub(" ", text))]
    if words:
        reasons.append(f"rule 2 (code/discount/refund/free words): {', '.join(words)}")

    rest = text
    links = []
    for m in _EMAIL_RE.finditer(text):
        if m.group(0).lower() != BRAND_EMAIL:
            links.append(m.group(0))
    rest = _EMAIL_RE.sub(" ", rest)
    for m in _HANDLE_RE.finditer(rest):
        if m.group(1).rstrip(".").lower() != BRAND_HANDLE:
            links.append("@" + m.group(1))
    for m in _URL_RE.finditer(rest):
        if not _link_allowed(m.group(0)):
            links.append(m.group(0))
    rest = _URL_RE.sub(" ", rest)
    for m in _BARE_DOMAIN_RE.finditer(rest):
        if not _link_allowed(m.group(0)):
            links.append(m.group(0))
    if links:
        reasons.append(f"rule 3 (link/domain not allowed): {', '.join(links)}")

    meta = [m.group(0) for m in _META_RE.finditer(text)]
    if meta:
        reasons.append(f"rule 4 (talks about its own setup): {', '.join(meta)}")

    return reasons
