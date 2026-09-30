"""Output guard for AUTO replies (audit T1-4, full; approved policy
statements: audit finding 7).

Pure-Python check run on the model's reply before an Instagram AUTO reply
is sent. It never rewrites text: a reply that trips any rule is held back
and routed to DRAFT+APPROVE by the caller (app._ig_output_guard), so the
founder sees exactly what the model wrote.

Rules (each returns a short reason naming the rule and what matched):
  1. A ₹ / Rs / INR amount that isn't allowed. The caller passes the live
     Shopify prices; FIXED_ALLOWED_INR adds brain.md's fixed amounts. An
     order total also passes when it is a sum of up to MAX_ORDER_ITEMS
     live prices (repeats allowed) and at most ₹1,500 (the Hard Money
     Threshold) — larger totals still go to a draft.
  2. code, coupon, promo, discount, refund, cashback or "free", judged
     sentence by sentence (_rule2_words): a clause with one of these words
     passes only when it is one of brain.md's approved policy statements —
     free shipping above ₹799, no discount / no codes, the refund
     timeline, shipping charges being non-refundable, a replacement or
     refund "once verified" (rule 5). "cruelty-free" and
     the discount decline pass wherever they appear, as before. Any other
     clause with one of the words holds the whole reply.
  3. A link, domain, email or @handle other than glamshelf.in,
     instagram.com/glamshelfstore, @glamshelfstore and the brand email.
  4. Prompt-injection phrasing: "system prompt", "my/your instructions",
     "ignore previous", QA mode or classifying. Plain "instructions"
     ("care instructions") and "brain" in product text pass.
  5. A replacement, reshipment or exchange (replace, reship, resend,
     exchange, "send you a new one", Hinglish "naya bhej denge"...), judged
     clause by clause like rule 2 (audit findings 2 and 9: the founder alone
     promises these, through approved drafts). Only the store policy's own
     statements pass: "Once verified, we'll arrange a replacement or refund
     at no additional cost" and the exchange offer. Refund promises were
     already held by rule 2; that policy sentence now passes rule 2 too.
  6. A reply that mentions free shipping (or ₹799) and also suggests
     adding or combining products to reach it — "if you add a set", "the
     Duo or a tray would get you there", "have you considered the Mink
     Duo" (audit finding 12: Twin states the threshold only, and its
     combination maths was wrong).
  7. A follow-up promise (audit finding 6): Twin can't message anyone
     later, so "the team will update you", "we'll remind you", "we'll be
     right here", "we'll take it from there", "we'll get back to you" and
     their Hinglish forms are held. The handoff and ack lines' "they'll /
     the team will reply here" and the LEAD line's "Udit will message you
     personally" pass only when the caller says a founder notice goes out
     with the reply (`founder_notice`: an ORDER / RESTOCK heads-up or a
     LEAD notice); on a plain AUTO reply nobody is told, so they're held
     too.

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

# Rule 1 order totals: "2 GS1 trays" style sums of live prices, up to this
# many items and no higher than the Hard Money Threshold.
MAX_ORDER_ITEMS = 6

BRAND_EMAIL = "glamshelfstore@gmail.com"
BRAND_HANDLE = "glamshelfstore"

# ₹849 / ₹ 1,500 / Rs. 849 / INR 849 / ₹849.00
_AMOUNT_RE = re.compile(r"(?:₹|\bRs\.?|\bINR)\s?(\d[\d,]*\d|\d)(\.\d{1,2})?", re.IGNORECASE)

# Rule 2 trigger words.
_PROMO_WORDS_RE = re.compile(
    r"\b(codes?|coupons?|promos?|promo[- ]?codes?|discount\w*|refund\w*|cashbacks?|free\w*)\b",
    re.IGNORECASE,
)
# Exempt wherever they appear, as since T1-4: cruelty-free (a product fact)
# and the discount decline — neither promises anything. Free shipping used
# to be here too; it now needs the ₹799 threshold (_FREE_SHIPPING_RULE).
_EXEMPT_PHRASES_RE = re.compile(
    r"cruelty[- ]free"
    r"|no (?:additional|extra|other) discounts?(?: (?:available|at the moment|right now))?"
    r"|no active discount codes?",
    re.IGNORECASE,
)

# Rule 2 is judged per sentence, and within a sentence per clause. A
# sentence ends at . ! ? before a space or a capital ("₹849.Free shipping"
# is two; "glamshelf.in" and "₹849.00" aren't split) or at a line break. A
# clause ends at a dash, a semicolon, or a comma before and / but / so /
# plus / though / with.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|(?<=[.!?])(?=[A-Z])|\n+")
_CLAUSE_SPLIT_RE = re.compile(r"\s+[—–-]\s+|;\s+|,\s+(?=(?:and|but|so|plus|though|with)\b)", re.IGNORECASE)

# Rule 2 approved statements (audit finding 7). Each pattern is matched
# against a clause lower-cased, with dashes as "-" and ₹799 written out
# (_normalize). They come from brain.md's own templates plus a few common
# phrasings; anything else with a trigger word is held.
_T = r"₹799"
_LEAD = r"(?:(?:and|but|plus|also|so|with) )?"
_FREE_SHIPPING = r"(?:free shipping|shipping(?:'s| is)(?: also)? free|(?:it |this |your order |the order )?ships (?:for )?free)"
_APPLIES = r"(?: (?:does |also )?appl(?:y|ies))?"

# Free shipping with the ₹799 threshold stated as the rule — true for every
# order. brain.md:289, 293, 555, 606. "free shipping on any order" (no
# threshold) matches nothing and is held.
_FREE_SHIPPING_RULE = (
    rf"{_LEAD}(?:we (?:do )?offer |you get )?{_FREE_SHIPPING}{_APPLIES}(?: on (?:all |any )?orders)? (?:above|over) {_T}(?: though)?",
    rf"{_LEAD}if (?:your|the) (?:order|total|cart) is (?:above|over) {_T},? (?:it ships (?:for )?free|shipping is free|you get free shipping)",
    # The threshold said other ways (finding 12: "state the threshold only";
    # main held "Orders above ₹799 ship free" and "Free shipping kicks in
    # above ₹799").
    rf"{_LEAD}(?:all |any )?(?:orders|anything) (?:above|over) {_T} (?:ships?|get|gets) (?:for )?free(?: shipping)?",
    rf"{_LEAD}free shipping (?:kicks in|starts) (?:above|over|from|on orders above|on orders over) {_T}",
)
# Free shipping because THIS order is above ₹799 — brain.md:921-923, and
# the 20+ tray bulk quote, brain.md:552. The first two pass only when the reply
# names no order amount under ₹799 (finding 12: "add the ₹499 set to your
# ₹299 pair and it ships free since it's above ₹799" is wrong maths).
_FREE_SHIPPING_THIS_ORDER = (
    rf"{_LEAD}(?:you get |you'll get )?{_FREE_SHIPPING}{_APPLIES},? (?:since|as|because) (?:it's|it is|that's|your order is|the order is|you're)(?: well)? (?:above|over) {_T}(?: though)?",
    rf"{_LEAD}(?:since|as|because) (?:your|the) (?:total|order|cart)(?: total)? is (?:well )?(?:above|over) {_T},? (?:shipping is free|it ships (?:for )?free|you get free shipping)",
)
_FREE_SHIPPING_BULK = (
    rf"{_LEAD}shipping is free,? since an order that size is well above {_T}",
)
# The same fact without the ₹799 figure — "and shipping is free on an order
# that size". It passes only in a reply that quotes the ₹749/tray bulk rate,
# which starts at 20 trays (₹14,980), so the order is always above ₹799.
_FREE_SHIPPING_BULK_SIZE = (
    rf"{_LEAD}(?:shipping is (?:also )?free|it ships (?:for )?free) (?:on|for) (?:an order|orders|a bulk order|bulk orders) (?:of )?(?:that|this) size",
)
_BULK_QUOTE_RE = re.compile(r"₹\s?749\s?(?:/\s?tray|per tray)", re.IGNORECASE)
# Below the threshold — says shipping ISN'T free, brain.md:923.
# "mark", "limit" and "minimum" are the same threshold (PR 50's re-check:
# "it's under the ₹799 free-shipping mark" was held).
_THRESHOLD_WORD = r"(?:threshold|mark|limit|minimum)"
_UNDER_THRESHOLD = (
    rf"(?:is|it's|it is|falls|fall|would fall|comes to|comes in)(?: just)? (?:under|below) (?:our |the )?"
    rf"(?:{_T} free[- ]shipping {_THRESHOLD_WORD}|free[- ]shipping {_THRESHOLD_WORD} of {_T})",
)

# No discount / no codes — brain.md:606 ("there's no additional discount
# available at the moment") and its code variants.
_NEG = r"(?:(?:and|but|so) )?"
_CODES = r"(?:coupon|discount|promo)(?: codes?)?"
_EXTRA = r"(?: (?:or|and) (?:(?:additional|extra|first-order|first order) )*(?:discounts?|offers?|coupon codes?|promo codes?))?"
_WHEN = r"(?: (?:available|running|active))?(?: (?:at the moment|right now|currently|for now))?"
_NO_DISCOUNT = (
    rf"{_NEG}(?:there's |there is |there are )?no (?:additional|extra|other|further) discounts?{_EXTRA}{_WHEN}",
    rf"{_NEG}(?:there's |there is |there are )?no (?:active )?{_CODES}{_EXTRA}{_WHEN}",
    rf"{_NEG}we don't have any (?:active )?{_CODES}{_EXTRA}{_WHEN}",
)

# Damaged / wrong item — the store's refund policy page and brain.md's
# damage template: a replacement or refund only "once verified". Approved in
# rule 2 (refund) and rule 5 (replacement). "We'll arrange a replacement
# for you right away" matches nothing and is held.
_ONCE_VERIFIED = (
    rf"{_LEAD}once (?:verified|we(?:'ve| have)? verified (?:it|them|the photos)),? "
    r"we(?:'ll| will) arrange a replacement or (?:a )?refund(?: at no (?:additional|extra) cost)?",
)
# The exchange offer — brain.md's exchange template and the policy page.
_EXCHANGE_OFFER = (
    rf"{_LEAD}we (?:do )?offer exchanges (?:on|for) eligible products,? subject to availability",
)

# Shipping charges aren't refunded — brain.md's refund notes and the
# store's refund policy page.
_NON_REFUNDABLE = (
    rf"{_LEAD}(?:the )?shipping (?:charges?|fees?|costs?)(?: \((?:if paid|if any)\)|,? (?:if paid|if any),?)? "
    r"(?:(?:are|is) (?:non-refundable|not refundable)|(?:aren't|isn't) refundable)",
)

# The refund timeline — brain.md:442-445 and the store's refund policy
# page: initiated within 24-48 hours of approval, then 5-7 working days to
# UPI/bank and 7-10 working days to cards ("business days" = "working
# days"). Every form carries an approval condition: "your refund will be
# initiated in 24-48 hours" (a promise, brain.md:878) matches nothing.
_COND = r"(?:(?:once|if|after) (?:(?:a |the |your )?(?:return|refund|it)(?: is|'s)? )?approved[,:]? )"
_COND_AFTER = (r"(?:(?:once|after|when) (?:(?:a |the |your )?(?:return|refund|it)(?: is|'s)? (?:received and )?approved"
               r"|we (?:receive and )?approve (?:the |your )?(?:return|refund|it)))")
_SUBJ = r"(?:(?:the |your )?refunds? (?:is |are |gets? )?)"
_INIT = r"initiated within 24-48 (?:hours|hrs)"
_WE_INIT = r"we initiate (?:it|the refund|your refund|refunds) within 24-48 (?:hours|hrs)"
_UPI_DAYS = r"5-7 (?:working|business) days"
_CARD_DAYS = r"7-10 (?:working|business) days"
_DEST = (r"(?:(?:your |the )?(?:original payment method|upi/bank(?: accounts?)?|bank/upi(?: accounts?)?"
         r"|upi or bank(?: accounts?)?|bank or upi(?: accounts?)?|bank accounts?|upi|bank|account))")
_CARD = r"(?:(?:your |the )?(?:cards?|card payments?|credit/debit cards?))"
_REFLECT = (
    rf"(?:(?:takes?|reach(?:es)?|reflects?(?: in| for| on)?|shows? up(?: in)?|lands? in|goes back to|go back to) "
    rf"{_DEST} (?:in|within) {_UPI_DAYS}(?:,? (?:and|or) (?:on |to |in |for )?{_CARD} (?:in|within) {_CARD_DAYS})?"
    rf"|takes? {_UPI_DAYS} to (?:reach|reflect in|reflect for|reflect on|show up in) {_DEST}"
    rf"(?:,? (?:and|or) {_CARD_DAYS} (?:for|on|to) {_CARD})?)"
)
_THEN = r"(?:,? (?:and |then |and then )?(?:it )?(?:then )?)"
_REFUND_TIMELINE = (
    rf"{_LEAD}{_COND}(?:{_SUBJ}|it's |it is |it gets ){_INIT}(?: of approval)?(?:{_THEN}{_REFLECT})?",
    rf"{_LEAD}{_COND}{_WE_INIT}(?:{_THEN}{_REFLECT})?",
    rf"{_LEAD}{_SUBJ}{_INIT} of approval(?:{_THEN}{_REFLECT})?",
    rf"{_LEAD}approved refunds (?:are |get )?{_INIT}(?:{_THEN}{_REFLECT})?",
    rf"{_LEAD}{_SUBJ}{_INIT} {_COND_AFTER}(?:{_THEN}{_REFLECT})?",
    rf"{_LEAD}(?:refunds?|the refund) (?:then )?{_REFLECT}",
    rf"{_LEAD}(?:refunds?|the refund) (?:then )?takes? (?:{_UPI_DAYS}(?: (?:for|on) {_DEST})?"
    rf"(?:,? (?:and|or) {_CARD_DAYS} (?:for|on) {_CARD})?|{_CARD_DAYS} (?:for|on) {_CARD})",
)
# From a refund statement to the end of its sentence, only these durations
# may appear: "…of approval, and processed within 7 business days" is held.
_DURATION_RE = re.compile(
    r"\b\d+(?:-\d+)?\s?(?:(?:working|business) )?(?:hours?|hrs?|days?|weeks?|months?)\b"
    r"|\b(?:instantly|immediately|right away|same[- ]day|today|tomorrow|overnight)\b"
)
_TIMELINE_DURATION_RE = re.compile(rf"24-48 (?:hours|hrs)|{_UPI_DAYS}|{_CARD_DAYS}")


def _compile(patterns, at_end: bool) -> tuple:
    """A statement starts at a word; it must end the clause (at_end) or be
    followed by a space or punctuation."""
    tail = r"$" if at_end else r"(?=$|[\s,.;:!?])"
    return tuple(re.compile(r"(?:^|(?<=\s))(?:" + p + r")" + tail) for p in patterns)


# Free shipping with the threshold may be followed by more words ("…above
# ₹799, which two trays would qualify for"); the others must end the clause.
_FREE_SHIPPING_STATEMENTS = _compile(_FREE_SHIPPING_RULE + _FREE_SHIPPING_BULK, at_end=False)
_BULK_SIZE_STATEMENTS = _compile(_FREE_SHIPPING_BULK_SIZE, at_end=True)
_THIS_ORDER_STATEMENTS = _compile(_FREE_SHIPPING_THIS_ORDER, at_end=False)
_CLAUSE_END_STATEMENTS = _compile(_NO_DISCOUNT + _NON_REFUNDABLE + _UNDER_THRESHOLD + _ONCE_VERIFIED, at_end=True)
_REFUND_STATEMENTS = tuple(re.compile(p) for p in _REFUND_TIMELINE)
_REPLACEMENT_STATEMENTS = _compile(_ONCE_VERIFIED + _EXCHANGE_OFFER, at_end=True)

# Rule 5 trigger words: a replacement, reshipment or exchange, in English
# or Hinglish.
_REPLACE_WORDS_RE = re.compile(
    r"\b(?:replac\w*|re-?ship\w*|re-?send\w*|exchang\w*)\b"
    r"|\b(?:send|ship|courier|dispatch)(?:ing)?\s+(?:you\s+)?(?:a\s+|the\s+)?"
    r"(?:new|fresh|another|correct|right)\b"
    r"|\b(?:naya|nayi|naye|dusra|doosra|dusri|doosri)\b.{0,20}?\bbhej\w*"
    r"|\bdobara\s+bhej\w*|\bbadal\s+(?:denge|dege|dunga|dungi|diya|dete)\b",
    re.IGNORECASE,
)

# Rule 6: a free-shipping mention, and a suggestion to add or combine
# products to reach it. Judged on the whole reply — "Free shipping applies
# above ₹799 — the Duo or a tray would get you there" puts the suggestion in
# another clause. "wouldn't qualify" and "your two trays would qualify" are
# statements about their own order, not suggestions, and pass.
_FREE_SHIP_MENTION_RE = re.compile(
    r"free[- ]?shipping|free delivery|ships? (?:for )?free|shipping(?:'s| is)? (?:also )?free"
    r"|₹\s?799\b|\brs\.?\s?799\b|\binr\s?799\b",
    re.IGNORECASE,
)
_COMBINATION_RE = re.compile(
    r"\bif you add\b|\badd(?:ing)? (?:a|an|another|one more|any|the|it|on)\b(?! to (?:your |the )?cart)"
    r"|\bget(?:s)? you (?:there|over|above|past)\b"
    r"|\b(?:push|take|bring)(?:es|s)? (?:you|it|the total|your total|your order) (?:over|above|past)\b"
    r"|\bto (?:reach|cross|hit|unlock|qualify for) (?:the )?(?:₹\s?799|free)"
    r"|\bhave you considered\b|\byou could (?:add|get|pick|also)\b|\bupgrade to\b"
    r"|\btop(?:ping)? (?:it )?up\b|\bpair it with\b|\bcombin(?:e|ing|ation)\b|\bcombo that\b"
    r"|\bor a (?:tray|set|duo|trio) (?:would|to|and)\b|\bwith (?:a|another|one more) (?:tray|set|duo|trio|pair)\b"
    r"|\badd kar\w*|\bsaath (?:mein|me)\b|\bmila(?:kar|ke)\b|\bek aur\b",
    re.IGNORECASE,
)

# Per-unit prices ("₹749/tray", "₹85 per pair") aren't order amounts.
_PER_UNIT_RE = re.compile(
    r"\s?(?:/\s?(?:tray|pair|set|wear)|per (?:tray|pair|set|wear)|a (?:pair|tray|wear)|each)",
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


# Rule 7: a promise that someone will contact the customer later, or keep
# something open for them. "I'll pass it to the team" (an action on their
# next message, e.g. "if you share your order ID") and capability talk ("we
# can…") aren't promises of a later contact and pass.
_FOLLOWUP_RE = re.compile(
    r"\b(?:we|i|the team|our team|team the glam shelf|they|someone|somebody|udit|our founder|the founder)"
    r"(?:'ll|'d| will| would| shall| (?:is|are|am) going to)\s+"
    r"(?:(?:personally|also|then|soon|shortly|definitely|surely|happily|just|quickly|directly)\s+)*"
    r"(?:update|remind|notify|follow[- ]?up|get back|reach out|contact|message|text|call|ping|"
    r"let you know|keep you (?:posted|updated|informed)|be in touch|get in touch|take it from there|"
    r"reply|respond|check back|circle back|look into|send you (?:a )?(?:reminder|message|update|note|dm))\b"
    r"|\b(?:we|i)(?:'ll| will) be (?:right )?here\b|\bget back to you\b"
    r"|\bwill (?:reply|get back|reach out|follow up|be in touch|update you|contact you|message you|remind you)\b"
    r"|\byou(?:'ll| will) hear (?:back )?from (?:us|the team|our team|udit)\b"
    r"|\b(?:remind|reminder|update|bata|contact|message|reply)\s+(?:bhi\s+)?(?:kar\s+)?"
    r"(?:denge|dunga|dungi|karenge|karega|karegi|karungi|karunga)\b"
    r"|\bteam\b.{0,40}?\b(?:karegi|karega|karenge|bataegi|bataenge|bata degi)\b",
    re.IGNORECASE,
)
# The follow-ups that are true when the founder is told: the handoff and
# ack lines ("they'll reply to you here", "the team will reply here") and
# the LEAD line.
_NOTIFIED_FOLLOWUP_RE = re.compile(
    r"\b(?:they|the team|our team|team the glam shelf)(?:'ll| will) reply (?:to you )?here\b"
    r"|\b(?:has|have) (?:your|the) [\w ]{0,30}? and will reply (?:to you )?here\b"
    r"|\budit will message you personally\b"
    # the same line in Hinglish: "(team / woh) yahan reply karenge"
    r"|\b(?:woh|wo|team)\s+(?:aapko\s+)?(?:yahan|yahin|yahi|idhar)\s+(?:hi\s+)?reply\s+kar(?:enge|egi|ega)\b",
    re.IGNORECASE,
)


def _followup_promises(text: str, founder_notice: bool) -> list[str]:
    """Rule 7's follow-up promises that aren't allowed here."""
    text = (text or "").replace("’", "'")
    allowed = [m.span() for m in _NOTIFIED_FOLLOWUP_RE.finditer(text)] if founder_notice else []
    return [m.group(0) for m in _FOLLOWUP_RE.finditer(text)
            if not any(s <= m.start() and m.end() <= e for s, e in allowed)]


def _amount_value(digits: str, fraction: str | None) -> float:
    value = float(digits.replace(",", ""))
    if fraction:
        value += float(fraction)
    return value


def _order_totals_paise(prices) -> set[int]:
    """Every sum of 1..MAX_ORDER_ITEMS live prices (repeats allowed) that is
    at most HARD_MONEY_THRESHOLD_INR, in paise so floats compare exactly.
    Built from the live prices only — never FIXED_ALLOWED_INR."""
    cap = HARD_MONEY_THRESHOLD_INR * 100
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


def _normalize(clause: str) -> str:
    """Lower-case, "-" for every dash, straight apostrophes, ₹799 however it
    was written, single spaces, no closing punctuation or 🤍."""
    c = clause.lower().replace("’", "'").replace("–", "-").replace("—", "-")
    c = re.sub(r"(?:rs\.?|inr)\s?799\b", "₹799", c)
    c = re.sub(r"₹\s+(?=\d)", "₹", c)
    c = re.sub(r"\s+", " ", c).strip(" \"'")
    return re.sub(r"[\s.!?🤍\"']+$", "", c)


def _order_amounts(text: str) -> list[float]:
    """₹ amounts in the reply that aren't per-unit prices."""
    return [_amount_value(m.group(1), m.group(2)) for m in _AMOUNT_RE.finditer(text or "")
            if not _PER_UNIT_RE.match(text, m.end())]


def _covered(clause: str, statements, words_re=_PROMO_WORDS_RE) -> bool:
    """One approved statement in the clause, and no trigger word outside it."""
    for pat in statements:
        for m in pat.finditer(clause):
            if not words_re.search(clause[:m.start()] + " " + clause[m.end():]):
                return True
    return False


def _approved(clause: str, rest_of_sentence: str, under_threshold_amount: bool,
              bulk_quote: bool = False) -> bool:
    if _covered(clause, _FREE_SHIPPING_STATEMENTS) or _covered(clause, _CLAUSE_END_STATEMENTS):
        return True
    if bulk_quote and _covered(clause, _BULK_SIZE_STATEMENTS):
        return True
    if _covered(clause, _THIS_ORDER_STATEMENTS) and not under_threshold_amount:
        return True
    if any(p.fullmatch(clause) for p in _REFUND_STATEMENTS):
        return all(_TIMELINE_DURATION_RE.fullmatch(d)
                   for d in _DURATION_RE.findall(rest_of_sentence))
    return False


def _rule2_words(text: str) -> list[str]:
    """Rule 2's trigger words that no approved statement covers, judged
    sentence by sentence and clause by clause."""
    under_threshold = any(a < FREE_SHIPPING_THRESHOLD_INR for a in _order_amounts(text))
    bulk_quote = bool(_BULK_QUOTE_RE.search(text or ""))
    left: list[str] = []
    for sentence in _SENTENCE_SPLIT_RE.split(text or ""):
        clauses = [_EXEMPT_PHRASES_RE.sub(" ", c) for c in _CLAUSE_SPLIT_RE.split(sentence)]
        normalized = [_normalize(c) for c in clauses]
        for i, clause in enumerate(clauses):
            words = [m.group(0) for m in _PROMO_WORDS_RE.finditer(clause)]
            if words and not _approved(normalized[i], " ".join(normalized[i:]), under_threshold, bulk_quote):
                left.extend(words)
    return left


def _rule5_words(text: str) -> list[str]:
    """Rule 5's replacement / reshipment words that no approved policy
    statement covers, judged clause by clause like rule 2."""
    left: list[str] = []
    for sentence in _SENTENCE_SPLIT_RE.split(text or ""):
        for clause in _CLAUSE_SPLIT_RE.split(sentence):
            words = [m.group(0) for m in _REPLACE_WORDS_RE.finditer(clause)]
            if words and not _covered(_normalize(clause), _REPLACEMENT_STATEMENTS, _REPLACE_WORDS_RE):
                left.extend(words)
    return left


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


def check_reply(text: str, allowed_amounts, founder_notice: bool = False) -> list[str]:
    """Return one reason per rule that fired ([] = the reply may be sent).
    `allowed_amounts` are the live product prices; FIXED_ALLOWED_INR is
    always added, and so are order totals up to ₹1,500 built from the live
    prices (_order_totals_paise). `founder_notice`: a founder notice goes
    out with this reply (rule 7)."""
    text = text or ""
    allowed = set(FIXED_ALLOWED_INR) | {float(a) for a in allowed_amounts or ()}
    totals = _order_totals_paise(allowed_amounts)
    reasons: list[str] = []

    def amount_ok(value: float) -> bool:
        return value in allowed or round(value * 100) in totals

    bad = [m.group(0) for m in _AMOUNT_RE.finditer(text)
           if not amount_ok(_amount_value(m.group(1), m.group(2)))]
    if bad:
        reasons.append(f"rule 1 (₹ amount not allowed): {', '.join(bad)}")

    words = _rule2_words(text)
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

    promised = _rule5_words(text)
    if promised:
        reasons.append(f"rule 5 (replacement/reshipment promise): {', '.join(promised)}")

    if _FREE_SHIP_MENTION_RE.search(text):
        combos = [m.group(0) for m in _COMBINATION_RE.finditer(text)]
        if combos:
            reasons.append(f"rule 6 (suggests products to reach free shipping): {', '.join(combos)}")

    promises = _followup_promises(text, founder_notice)
    if promises:
        reasons.append(f"rule 7 (follow-up promise): {', '.join(promises)}")

    return reasons
