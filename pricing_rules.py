"""Deterministic resolution for the bulk-rate vs Hard Money Threshold rule pair.

brain.md v1.9 and earlier had a contradiction: Rule 3b said a rate-only ask
for 20+ trays is AUTO (state ₹749/tray), while the Hard Money Threshold's
coverage list included "bulk order quotes" — and every 20+ tray quote implies
>₹1,500, so the top-down evaluation order escalated every bulk inquiry and
Rule 3b never fired.

Founder-confirmed resolution (July 12, 2026, brain.md v1.10):
  - Informational rate-sharing NEVER escalates on amount grounds, no matter
    how large the implied total. Ask = AUTO.
  - The Hard Money Threshold applies only to actual commitments/transactions:
    orders being placed, refunds, replacements, discount codes actually
    issued. Commit >₹1,500 = ESCALATE.
  - A commit signal for 20+ trays always escalates (Rule 3b-i — the founder
    finalises every bulk deal), independent of the threshold.

This module is pure (no Flask, no env reads, no I/O) so it can be unit-tested
in isolation. app.py wires it into the deterministic pre-filter stage of
draft_reply_logic(), where — like the escalation phrase filter — it can only
UPGRADE the LLM's classification to ESCALATE, never downgrade it.
"""

import re

# Pricing constants — keep in lockstep with brain.md Section 2.
# ₹749 standard bulk quote (20+ trays); ₹1,500 Hard Money Threshold
# (strictly above → escalate, per the worked examples in brain.md).
BULK_RATE_INR = 749
BULK_MIN_TRAYS = 20
HARD_MONEY_THRESHOLD_INR = 1500

INTENT_ASK = "ask"
INTENT_COMMIT = "commit"

ACTION_AUTO = "AUTO"
ACTION_ESCALATE = "ESCALATE"


def resolve_pricing_action(quantity: int | None, amount: float | None, intent: str) -> str:
    """Resolve the bulk-rate / Hard Money Threshold rule pair to ONE action.

    Args:
      quantity: number of trays in play, or None if not stated.
      amount:   order/transaction value in INR, or None if not determinable.
                Callers should pass the implied value (quantity x rate) when
                they can compute it, but must not guess.
      intent:   INTENT_ASK    — customer is asking about pricing (informational)
                INTENT_COMMIT — customer is placing/committing to a transaction

    Returns ACTION_AUTO or ACTION_ESCALATE.

    Decision table (founder-confirmed):
      ask                                  -> AUTO   (rate-sharing never escalates
                                                      on amount grounds; Rule 3b)
      commit, quantity >= 20               -> ESCALATE (Rule 3b-i: founder closes
                                                        every bulk deal)
      commit, amount > 1,500               -> ESCALATE (Hard Money Threshold on a
                                                        real transaction)
      commit, below both lines             -> AUTO   (normal small-order handling)
    """
    if intent not in (INTENT_ASK, INTENT_COMMIT):
        raise ValueError(f"intent must be {INTENT_ASK!r} or {INTENT_COMMIT!r}, got {intent!r}")

    if intent == INTENT_ASK:
        return ACTION_AUTO

    if quantity is not None and quantity >= BULK_MIN_TRAYS:
        return ACTION_ESCALATE
    if amount is not None and amount > HARD_MONEY_THRESHOLD_INR:
        return ACTION_ESCALATE
    return ACTION_AUTO


# Commit phrases are the founder-confirmed examples from brain.md Rule 3b-i
# ("ok I'll take 50", "let's do 30", "how do I pay for 25"). Only patterns
# that carry an explicit quantity belong here — "book it" is also a commit
# signal but has no number, so it stays with the LLM, which has conversation
# history to judge it. Do not extend this list with unconfirmed phrasings.
_BULK_COMMIT_PATTERNS = re.compile(
    r"\b(?:"
    r"i[’']?ll\s+take\s+(\d+)"
    r"|let[’']?s\s+do\s+(\d+)"
    r"|how\s+do\s+i\s+pay\s+for\s+(\d+)"
    r")\b",
    re.IGNORECASE,
)


def detect_bulk_commit_quantity(message: str) -> int | None:
    """Return the quantity from an explicit commit phrase, or None.

    None means "no deterministic commit signal", not "this is an ask" —
    ambiguous messages are left to the LLM classification.
    """
    m = _BULK_COMMIT_PATTERNS.search(message or "")
    if not m:
        return None
    qty = next(g for g in m.groups() if g is not None)
    return int(qty)
