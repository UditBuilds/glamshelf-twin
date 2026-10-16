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
# Every tray holds 10 pairs, so the bulk rate starts at 200 pairs. A
# quantity in pairs is converted before any bulk decision (audit finding 3:
# "20 pairs" got the 20+ tray quote).
PAIRS_PER_TRAY = 10
# brain.md's free-shipping line for a bulk quote (Output Contract), which
# the model must use word for word.
BULK_FREE_SHIPPING_LINE = "and shipping is free, since an order that size is well above ₹799"

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
# A bare number means trays (the founder's examples); "pairs" right after
# it is converted (finding 3: "I'll take 25 pairs" used to count as 25 trays).
_BULK_COMMIT_PATTERNS = re.compile(
    r"\b(?:"
    r"i[’']?ll\s+take\s+(\d+)"
    r"|let[’']?s\s+do\s+(\d+)"
    r"|how\s+do\s+i\s+pay\s+for\s+(\d+)"
    r")\b(?:\s*(pairs?|trays?)\b)?",
    re.IGNORECASE,
)

# A number with its unit: "20 pairs", "25 trays", "3 tray". "10 pairs each"
# / "10 pairs per tray" describe a tray, not an order, and don't count.
_QUANTITY_RE = re.compile(
    r"\b(\d{1,5})\s*(pairs?|trays?)\b(?!\s+(?:each|per\b|in (?:a|one|each|every) tray))",
    re.IGNORECASE,
)
# "20+ trays" / "200+ pairs": the plus keeps _QUANTITY_RE from seeing them.
# They count as their number (Argon finding 5).
_PLUS_QUANTITY_RE = re.compile(r"\b(\d{1,5})\s*\+\s*(pairs?|trays?)\b", re.IGNORECASE)


def _as_trays(count: int, unit: str) -> int | float:
    """`count` pairs or trays, in trays: 20 pairs -> 2, 25 pairs -> 2.5."""
    if not (unit or "").lower().startswith("pair"):
        return count
    trays = count / PAIRS_PER_TRAY
    return int(trays) if trays.is_integer() else trays


def detect_bulk_commit_quantity(message: str) -> int | float | None:
    """Return the quantity in trays from an explicit commit phrase, or None.

    "I'll take 200 pairs" is 20 trays; "I'll take 25 pairs" is 2.5 trays —
    never 25. None means "no deterministic commit signal", not "this is an
    ask" — ambiguous messages are left to the LLM classification.
    """
    m = _BULK_COMMIT_PATTERNS.search(message or "")
    if not m:
        return None
    qty = next(g for g in m.groups()[:3] if g is not None)
    return _as_trays(int(qty), m.group(4))


# Several quantities add up only when they are clearly extra items (Argon
# finding 4: "10 trays vs 12 trays" used to count as 22 and get the bulk
# note). Between two quantities, "and" / "+" / "plus" / "aur" — after at
# most an item description ("of GS1", "for the salon") — adds; so does a
# comma list naming different products. An alternative or restatement
# marker, a past order, or anything else means the largest single quantity:
# when in doubt the system must not claim 20+ trays.
_ALTERNATIVE_RE = re.compile(
    r"\b(?:or|vs|versus|ya|means?|is|are|equals?|i\.?e|aka|matlab|instead|either)\b|[/=]",
    re.IGNORECASE,
)
_PAST_ORDER_RE = re.compile(
    r"\b(?:last|ago|earlier|before|previous(?:ly)?|already|bought|ordered|tried|used|had)\b",
    re.IGNORECASE,
)
_PRODUCT = r"(?:gs\s?\d|clean\s+girl|kawaii|mink\s+(?:duo|trio)|(?:everyday\s*(?:\+|and|&)\s*)?glam\s+duo)"
_WORD = rf"(?:{_PRODUCT}|[a-z'’-]+)"
_ITEM = rf"(?:(?:of|for|in)\s+{_WORD}(?:\s+{_WORD}){{0,4}}|{_PRODUCT})"
_ADD_GAP_RE = re.compile(rf"\s*(?:{_ITEM})?\s*,?\s*(?:and|&|\+|plus|aur)\s+(?:also\s+)?", re.IGNORECASE)
# Nothing but a connector between two quantities: "10 trays and 100 pairs".
_BARE_GAP_RE = re.compile(r"\s*,?\s*(?:(?:and|&|\+|plus|aur)\s+(?:also\s+)?)?", re.IGNORECASE)
_LIST_GAP_RE = re.compile(rf"\s+(?:of\s+)?({_PRODUCT})\s*,\s*", re.IGNORECASE)
_LIST_NEXT_RE = re.compile(rf"\s+(?:of\s+)?({_PRODUCT})\b", re.IGNORECASE)


def _quantities(message: str) -> list[tuple[int, str, int, int, bool]]:
    """Each pairs / trays quantity in the message, in order, as (count,
    unit, start, end, plus); plus is True for "20+ trays"."""
    found = [(int(m.group(1)), m.group(2), m.start(), m.end(), plus)
             for regex, plus in ((_QUANTITY_RE, False), (_PLUS_QUANTITY_RE, True))
             for m in regex.finditer(message or "")]
    return sorted(found, key=lambda q: q[2])


def _shown(count: int | float, plus: bool) -> str:
    return f"{count:g}{'+' if plus else ''}"


def _restates(a, b) -> bool:
    """One quantity in pairs, the other in trays, and the same amount:
    "10 trays" and "100 pairs"."""
    return (a[1][0].lower() != b[1][0].lower()
            and _as_trays(a[0], a[1]) == _as_trays(b[0], b[1]))


def _adds_up(message: str, a, b) -> bool:
    """Is quantity b clearly an extra item on top of quantity a?"""
    gap = message[a[3]:b[2]]
    if _ALTERNATIVE_RE.search(gap) or _PAST_ORDER_RE.search(gap):
        return False
    if _ADD_GAP_RE.fullmatch(gap):
        return True
    listed, following = _LIST_GAP_RE.fullmatch(gap), _LIST_NEXT_RE.match(message, b[3])
    return bool(listed and following
                and re.sub(r"\W", "", listed.group(1).lower()) != re.sub(r"\W", "", following.group(1).lower()))


def _tally(message: str) -> tuple[int | float | None, str, list, bool]:
    """(trays, how, amounts, plus). `how` is "one" (a single quantity),
    "sum" (clearly extra items, added up), "same" (one amount said twice)
    or "max" (anything else: the largest single quantity). A pairs number
    equal to a trays number x10 with nothing but a connector between them
    ("10 trays and 100 pairs") counts once. `plus`: the count includes a
    "20+ trays" style amount."""
    found = _quantities(message)
    if not found:
        return None, "", [], False
    groups = [found[0]]
    adds = []
    for prev, q in zip(found, found[1:]):
        if _restates(prev, q) and _BARE_GAP_RE.fullmatch(message[prev[3]:q[2]]):
            continue
        adds.append(_adds_up(message, prev, q))
        groups.append(q)
    amounts = [_as_trays(q[0], q[1]) for q in groups]
    if len(amounts) > 1 and all(adds):
        total, how = sum(amounts), "sum"
        plus = any(q[4] for q in groups)
    else:
        total = max(amounts)
        how = "one" if len(found) == 1 else ("same" if len(set(amounts)) == 1 else "max")
        plus = any(q[4] for q, a in zip(groups, amounts) if a == total)
    return (int(total) if float(total).is_integer() else total), how, amounts, plus


def trays_mentioned(message: str) -> int | float | None:
    """How many trays the message asks about, pairs converted at
    PAIRS_PER_TRAY per tray: "20 pairs" -> 2, "200 pairs" -> 20, "25 trays"
    -> 25. Quantities add up only when they're clearly extra items ("10
    trays of GS1 and 10 trays of GS2" -> 20); alternatives and restatements
    ("10 trays vs 12 trays", "is 10 trays 100 pairs?") give the largest
    single quantity. "20+ trays" and "200+ pairs" count as 20 (Argon
    finding 5). None when no number carries a pairs or trays unit."""
    return _tally(message)[0]


def bulk_trays_asked(message: str) -> int | float | None:
    """The trays a message asks about (trays_mentioned) when that's 20+
    (200+ pairs, at PAIRS_PER_TRAY per tray), else None. Used for the
    founder's LEAD notice on a bulk question (founder decision, 3 Oct
    2026); it never changes the reply."""
    trays = trays_mentioned(message)
    return trays if trays is not None and trays >= BULK_MIN_TRAYS else None


def bulk_quantity_note(message: str) -> str:
    """The pairs-to-trays conversion and bulk verdict for the customer's own
    numbers, written for the model ("" when there's nothing to convert).
    The model doesn't do arithmetic reliably (audit finding 3: "20 pairs"
    got the 20+ tray quote word-for-word 3/3), so the system does it and
    app.build_user_message adds the line to the prompt. Only digits from the
    message reach the note — never the customer's words. A pair or two
    ("2 pairs of Kawaii") is a singles question and gets no note. Several
    quantities follow trays_mentioned: added up only when they're clearly
    extra items, else the largest (Argon finding 4)."""
    found = _quantities(message)
    if not found:
        return ""
    pairs = [q for q in found if q[1].lower().startswith("pair")]
    total, how, amounts, plus = _tally(message)
    # A pair or two is a singles question; one tray is a plain price question.
    if (not pairs and total < 2) or (len(pairs) == len(found) and total < 1):
        return ""
    shown = _shown(total, plus)
    parts = []
    for n, unit, _, _, more in found:
        if unit.lower().startswith("pair"):
            parts.append(f"{_shown(n, more)} pairs = {_shown(_as_trays(n, unit), more)} trays")
        else:
            parts.append(f"{_shown(n, more)} {'tray' if n == 1 and not more else 'trays'}")
    line = "; ".join(parts)
    if how == "sum":
        line += f" — {shown} trays in all"
    elif how == "same":
        line += f" — the same amount said twice: {shown} trays"
    elif how == "max":
        line += f" — these aren't added up; the largest is {shown} trays"
    if pairs:
        line += " (trays come in 10 pairs each)"
    if total >= BULK_MIN_TRAYS:
        # The free-shipping fact in brain.md's own words: the model's
        # paraphrases ("shipping is free at that size") are held by the
        # output guard (rule 2), which turns the answer into a handoff.
        verdict = (f"That is 20+ trays, so the ₹{BULK_RATE_INR}/tray bulk rate applies. With the rate, "
                   f"say word for word: \"{BULK_FREE_SHIPPING_LINE}\".")
        if how == "max" and min(amounts) < BULK_MIN_TRAYS:
            verdict += f" The ₹{BULK_RATE_INR} rate is only for the {shown} trays, not the smaller amount."
    else:
        verdict = (f"That is under 20 trays (200 pairs), so the ₹{BULK_RATE_INR} bulk rate "
                   "does NOT apply — the regular tray price does.")
    return f"{line}. {verdict}"
