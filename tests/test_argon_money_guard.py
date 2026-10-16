"""Money and promise fixes from the outside audit (Argon findings 4-6,
Argon tests 7 and 14), checked against the code.

Finding 4: trays_mentioned() added up every quantity in a message, so
alternatives and restatements ("10 trays vs 12 trays", "is 10 trays 100
pairs?") got the ₹749 bulk note.

No live API call anywhere here.

Run:  python -m unittest tests.test_argon_money_guard
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pricing_rules import (  # noqa: E402
    BULK_FREE_SHIPPING_LINE,
    bulk_quantity_note,
    bulk_trays_asked,
    trays_mentioned,
)

BRAIN = (REPO / "brain" / "brain.md").read_text(encoding="utf-8")


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
        self.assertIn("The ₹749 rate is only for the 25-tray amount, not the smaller one.", note)

    def test_a_bulk_ask_with_no_quantity_still_gets_no_note(self):
        # brain.md: ask for the quantity first, never ₹749 upfront.
        self.assertIn("If a customer asks about bulk/MUA pricing **without mentioning quantity**, twin MUST "
                      "ask for quantity first. Never quote ₹749 upfront.", BRAIN)
        for message in ("whats ur bulk price?", "I'm a MUA, bulk rate kya hai?", "do you do bulk orders for MUAs?"):
            with self.subTest(message=message):
                self.assertEqual(bulk_quantity_note(message), "")
                self.assertIsNone(trays_mentioned(message))
                self.assertIsNone(bulk_trays_asked(message))


if __name__ == "__main__":
    unittest.main()
