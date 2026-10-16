"""Money and promise fixes from the outside audit (Argon findings 4-6,
Argon tests 7 and 14), checked against the code.

Finding 4: trays_mentioned() added up every quantity in a message, so
alternatives and restatements ("10 trays vs 12 trays", "is 10 trays 100
pairs?") got the ₹749 bulk note. Finding 5: "20+ trays" was left out of
that note. Finding 6: output_guard.check_reply() sent money promises it
had no word for ("15% off", "shipping is on us", "600 rupees").

No live API call anywhere here.

Run:  python -m unittest tests.test_argon_money_guard
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import output_guard  # noqa: E402
from pricing_rules import (  # noqa: E402
    BULK_FREE_SHIPPING_LINE,
    bulk_quantity_note,
    bulk_trays_asked,
    trays_mentioned,
)

BRAIN = (REPO / "brain" / "brain.md").read_text(encoding="utf-8")
# brain.md's product table, the startup prices before the first Shopify fetch.
LIVE_PRICES = {249.0, 299.0, 499.0, 699.0, 849.0}


class Finding4AlternativesAreNotAddedUp(unittest.TestCase):
    CASES = (
        ("How much for 10 trays vs 12 trays?", 12),
        ("Confused — should I buy 10 trays of GS1 or 12 trays of GS2?", 12),
        ("Is 10 trays 100 pairs?", 10),
        ("Does 20 trays mean 200 pairs?", 20),
        ("price for 5 trays or 15 trays", 15),
    )

    def test_the_audit_examples(self):
        for message, trays in self.CASES:
            with self.subTest(message=message):
                self.assertEqual(trays_mentioned(message), trays)

    def test_different_products_still_add_up(self):
        self.assertEqual(trays_mentioned("10 trays of GS1 and 10 trays of GS2"), 20)
        self.assertEqual(bulk_trays_asked("10 trays of GS1 and 10 trays of GS2"), 20)
        self.assertIn("20 trays in all. That is 20+ trays", bulk_quantity_note("10 trays of GS1 and 10 trays of GS2"))
        for message, trays in (("10 trays + 10 trays", 20), ("10 trays plus 15 trays", 25),
                               ("10 trays aur 10 trays chahiye", 20),
                               ("10 trays of GS1, 5 trays of GS2 and 5 trays of GS3", 20),
                               ("5 trays for the salon and 50 pairs for my kit", 10)):
            with self.subTest(message=message):
                self.assertEqual(trays_mentioned(message), trays)

    def test_the_same_amount_said_twice_counts_once(self):
        for message, trays in (("is 20 trays and 200 pairs the same?", 20), ("10 trays and 100 pairs", 10),
                               ("10 trays = 100 pairs?", 10), ("10 trays/100 pairs", 10),
                               ("20 trays (200 pairs)", 20)):
            with self.subTest(message=message):
                self.assertEqual(trays_mentioned(message), trays)

    def test_when_unsure_the_largest_single_quantity(self):
        # A comma between two amounts of no named product, a past order,
        # anything that isn't clearly an extra item.
        for message, trays in (("10 trays, 12 trays", 12), ("I bought 15 trays last month and 10 trays", 15),
                               ("15 trays ya 10 trays?", 15), ("10 trays versus 15 trays", 15)):
            with self.subTest(message=message):
                self.assertEqual(trays_mentioned(message), trays)

    def test_the_note_never_claims_20_plus_for_the_examples(self):
        for message, trays in self.CASES:
            with self.subTest(message=message):
                note = bulk_quantity_note(message)
                self.assertNotIn("in all", note)
                if trays < 20:
                    self.assertIn("That is under 20 trays (200 pairs), so the ₹749 bulk rate does NOT apply", note)
                    self.assertNotIn(BULK_FREE_SHIPPING_LINE, note)
                    self.assertIsNone(bulk_trays_asked(message))
                else:
                    self.assertIn("That is 20+ trays", note)

    def test_the_note_says_how_it_counted(self):
        self.assertEqual(
            bulk_quantity_note("How much for 10 trays vs 12 trays?"),
            "10 trays; 12 trays — these aren't added up; the largest is 12 trays. That is under 20 trays "
            "(200 pairs), so the ₹749 bulk rate does NOT apply — the regular tray price does.")
        self.assertEqual(
            bulk_quantity_note("Is 10 trays 100 pairs?"),
            "10 trays; 100 pairs = 10 trays — the same amount said twice: 10 trays (trays come in 10 pairs "
            "each). That is under 20 trays (200 pairs), so the ₹749 bulk rate does NOT apply — the regular "
            "tray price does.")

    def test_a_20_plus_alternative_gets_the_rate_for_that_amount_only(self):
        note = bulk_quantity_note("How much for 10 trays vs 25 trays?")
        self.assertIn("the largest is 25 trays. That is 20+ trays", note)
        self.assertIn(f'"{BULK_FREE_SHIPPING_LINE}"', note)
        self.assertIn("The ₹749 rate is only for the 25 trays, not the smaller amount.", note)

    def test_a_bulk_ask_with_no_quantity_still_gets_no_note(self):
        # brain.md: ask for the quantity first, never ₹749 upfront.
        self.assertIn("If a customer asks about bulk/MUA pricing **without mentioning quantity**, twin MUST "
                      "ask for quantity first. Never quote ₹749 upfront.", BRAIN)
        for message in ("whats ur bulk price?", "I'm a MUA, bulk rate kya hai?", "do you do bulk orders for MUAs?"):
            with self.subTest(message=message):
                self.assertEqual(bulk_quantity_note(message), "")
                self.assertIsNone(trays_mentioned(message))
                self.assertIsNone(bulk_trays_asked(message))


class Finding5PlusQuantitiesCount(unittest.TestCase):
    """"20+ trays" / "200+ pairs" counted for the LEAD notice only; the
    model's note ignored them."""

    def assertBulkNote(self, note):
        self.assertIn("That is 20+ trays, so the ₹749/tray bulk rate applies.", note)
        self.assertIn(f'say word for word: "{BULK_FREE_SHIPPING_LINE}".', note)
        self.assertNotIn("does NOT apply", note)

    def test_a_past_purchase_and_a_20_plus_ask(self):
        message = "I tried 2 trays, what's the rate for 20+ trays?"
        self.assertEqual(trays_mentioned(message), 20)
        self.assertEqual(bulk_trays_asked(message), 20)
        note = bulk_quantity_note(message)
        self.assertBulkNote(note)
        self.assertTrue(note.startswith("2 trays; 20+ trays — these aren't added up; the largest is 20+ trays."))

    def test_a_plain_20_plus_ask(self):
        message = "What's the rate for 20+ trays?"
        self.assertEqual(trays_mentioned(message), 20)
        note = bulk_quantity_note(message)
        self.assertBulkNote(note)
        self.assertTrue(note.startswith("20+ trays. That is 20+ trays"))

    def test_pairs_with_a_plus(self):
        self.assertEqual(trays_mentioned("200+ pairs ka rate kya hai?"), 20)
        note = bulk_quantity_note("200+ pairs ka rate kya hai?")
        self.assertTrue(note.startswith("200+ pairs = 20+ trays (trays come in 10 pairs each)."))
        self.assertBulkNote(note)
        self.assertEqual(trays_mentioned("20+ trays (200+ pairs)"), 20)

    def test_under_20_with_a_plus_is_still_under(self):
        self.assertEqual(trays_mentioned("10+ trays ka rate?"), 10)
        self.assertIsNone(bulk_trays_asked("10+ trays ka rate?"))
        self.assertIn("does NOT apply", bulk_quantity_note("10+ trays ka rate?"))
        self.assertEqual(bulk_quantity_note("2+ pairs"), "")


def guard(text, founder_notice=False):
    return output_guard.check_reply(text, LIVE_PRICES, founder_notice=founder_notice)


class Finding6MoneyPromisesAreHeld(unittest.TestCase):
    """check_reply() returned [] for these, so they went out as AUTO."""

    def assertHeldBy(self, text, rule, matched):
        reasons = guard(text)
        self.assertTrue(any(r.startswith(rule) and matched in r for r in reasons), (text, reasons))

    def test_the_audit_examples(self):
        for text, rule, matched in (
            ("We can give you 15% off on this order!", "rule 2", "15% off"),
            ("Shipping is complimentary on your order.", "rule 2", "complimentary"),
            ("Shipping is on us for this one.", "rule 2", "on us"),
            ("Paise wapas mil jayenge.", "rule 2", "Paise wapas"),
            ("We will give you your money back.", "rule 2", "money back"),
            ("It costs 600 rupees per tray.", "rule 1", "600 rupees"),
            ("Each pair is ₹85 and a tray is ₹50.", "rule 1", "₹50"),
            ("Shipping is ₹50 to Mumbai.", "rule 1", "₹50"),
        ):
            with self.subTest(text=text):
                self.assertHeldBy(text, rule, matched)

    def test_more_money_words(self):
        for text, matched in (
            ("We'll waive the shipping fee for you 🤍", "waive"),
            ("We'll reimburse the shipping 🤍", "reimburse"),
            ("Flat ₹100 off on your next order 🤍", "₹100 off"),
            ("Get 20 percent off today", "20 percent off"),
            ("Aapke paisa wapas aa jayenge", "paisa wapas"),
            ("Shipping's on us 🤍", "on us"),
        ):
            with self.subTest(text=text):
                self.assertHeldBy(text, "rule 2", matched)

    def test_amounts_written_after_the_number(self):
        for text, matched in (("That's 600 rs per tray", "600 rs"), ("It's just 600/- for the tray", "600/-"),
                              ("GS1 is Rs 600/- only", "Rs 600")):
            with self.subTest(text=text):
                self.assertHeldBy(text, "rule 1", matched)
        # Written once, counted once; a live price still passes either way.
        self.assertEqual(guard("GS1 is Rs 849/-, GS2 is 849 rupees 🤍"), [])
        self.assertEqual(guard("Rs 600/-"), ["rule 1 (₹ amount not allowed): Rs 600"])

    def test_fixed_amounts_only_in_their_own_context(self):
        for text, matched in (("A tray is ₹85", "₹85"), ("Each pair is ₹85", "₹85"),
                              ("The Mink Duo is ₹12 per set", "₹12"), ("GS1 is ₹17", "₹17"),
                              ("Delivery is ₹50", "₹50"), ("The Kawaii pair is ₹50 off", "₹50")):
            with self.subTest(text=text):
                self.assertHeldBy(text, "rule 1", matched)

    def test_brain_md_uses_of_the_fixed_amounts_still_pass(self):
        for text in (
            "A tray works out to around ₹85 per pair, ~₹12–17 per wear 🤍",
            "That's ~₹85/pair and ₹12 to ₹17 per wear 🤍",
            "Just avoid the cheap ₹50 white glues 🤍",
            "Cheaper lashes typically have heavy bands, shed after one wear, and the glue won't stick a second "
            "time. Ours are reusable 5–7 times, lightweight, and a tray works out to around ₹85 per pair — "
            "the math genuinely works out better long-term 🤍",
        ):
            with self.subTest(text=text):
                self.assertEqual(guard(text), [])

    def test_normal_lash_talk_passes(self):
        for text in (
            "Peel the glue off gently, then take off the lash from the outer corner 🤍",
            "Let the glue dry for 30 seconds before you put them on, and take them off slowly 🤍",
            "You can always count on us for soft, lightweight lashes 🤍",
            "Glue off the lash band first, then store them back in the tray 🤍",
        ):
            with self.subTest(text=text):
                self.assertEqual(guard(text), [])

    def test_the_approved_statements_still_send(self):
        for text in (
            "Refunds are initiated within 24–48 hours of approval, then reach UPI/bank accounts in 5–7 working "
            "days and cards in 7–10 working days 🤍",
            "There are no active discount codes at the moment 🤍",
            "Our prices are already reduced from the original MRP — there's no additional discount available "
            "at the moment. Free shipping does apply on orders above ₹799 though 🤍",
            "Yes — 25 trays qualifies for our bulk rate of ₹749/tray (20+ trays, 200+ pairs) — "
            f"{BULK_FREE_SHIPPING_LINE} 🤍",
        ):
            with self.subTest(text=text):
                self.assertEqual(guard(text), [])


def product_list_from_brain() -> str:
    """brain.md's "Default product list reply" quoted block, as sent."""
    after = BRAIN.split("**Default product list reply (in-stock only):**", 1)[1]
    lines = []
    for line in after[after.index('> "'):].splitlines():
        if not line.startswith(">"):
            break
        lines.append(line[2:] if line.startswith("> ") else "")
    return "\n".join(lines).strip('"')


class Test7BulkFloorIsNotATrayPrice(unittest.TestCase):
    """₹699 is in FIXED_ALLOWED_INR (the founder-only bulk floor) and is
    the Mink Trio's live price, so a ₹699/tray offer passed rule 1."""

    def assertFloorHeld(self, text):
        reasons = guard(text)
        self.assertTrue(any(r.startswith("rule 1") and "bulk floor" in r for r in reasons), (text, reasons))

    def test_the_audit_example(self):
        self.assertEqual(guard("Best I can do is ₹699/tray for 40 trays"),
                         ["rule 1 (₹ amount not allowed): ₹699/tray (the founder-only bulk floor as a tray or pair price)"])

    def test_per_tray_and_per_pair_forms(self):
        for text in ("₹699/tray for 30 trays 🤍", "I can do ₹699 per tray", "That's ₹699 a tray 🤍",
                     "They're ₹699 each", "₹699/pair", "Rs 699 per pair", "699 rupees per tray",
                     # a per-unit ₹699 is never the Mink Trio's price, named or not
                     "The Mink Trio is ₹699/tray"):
            with self.subTest(text=text):
                self.assertFloorHeld(text)

    def test_in_a_sentence_about_trays(self):
        for text in ("For 40 trays we can do ₹699 🤍", "40 trays at ₹699 works for us",
                     "GS2 is ₹699 right now 🤍", "Trays are ₹849. For 40 trays I can go down to ₹699 🤍"):
            with self.subTest(text=text):
                self.assertFloorHeld(text)

    def test_the_mink_trio_still_passes(self):
        product_list = product_list_from_brain()
        self.assertIn("• Mink Trio — ₹699 · 3 faux mink pairs", product_list)
        self.assertIn("Trays (10 pairs)", product_list)
        for text in (product_list,
                     "The Mink Trio is ₹699 for 3 faux mink pairs 🤍",
                     "The Mink Trio is ₹699 and our trays are ₹849 🤍",
                     "MINK DUO — ₹499 | EVERYDAY + GLAM DUO — ₹499 | MINK TRIO — ₹699. GS1, GS2 & GS3 Luxe "
                     "Light Lash Trays (10 pairs each) — ₹849."):
            with self.subTest(text=text[:40]):
                self.assertEqual(guard(text), [])

    def test_the_bulk_quote_still_passes(self):
        self.assertEqual(guard("Our bulk rate is ₹749/tray for orders of 20+ trays (200+ pairs) — and shipping is "
                               f"{BULK_FREE_SHIPPING_LINE.split('shipping is ', 1)[1]} 🤍"), [])


if __name__ == "__main__":
    unittest.main()
