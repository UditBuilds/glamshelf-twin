"""Tests for pricing_rules.resolve_pricing_action / detect_bulk_commit_quantity.

The first three tests are the scenarios that triggered the brain.md v1.9
conflict: Rule 3b (bulk rate-ask = AUTO) vs the Hard Money Threshold, whose
coverage list included "bulk order quotes" — so the same message resolved to
two different classifications depending on which rule you read first. Each
must now resolve to exactly ONE action.

Run:  python -m unittest discover tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pricing_rules import (
    ACTION_AUTO,
    ACTION_ESCALATE,
    BULK_MIN_TRAYS,
    BULK_RATE_INR,
    INTENT_ASK,
    INTENT_COMMIT,
    PAIRS_PER_TRAY,
    detect_bulk_commit_quantity,
    resolve_pricing_action,
    trays_mentioned,
)


class ConflictScenarios(unittest.TestCase):
    """Messages that previously matched BOTH rules with contradictory outcomes."""

    def test_rate_only_ask_for_30_trays_is_auto(self):
        # "what's the rate for 30 trays?" — implied value ₹22,470 (>₹1,500).
        # v1.9: Rule 3b said AUTO, threshold coverage said ESCALATE, and the
        # top-down evaluation order made ESCALATE win — Rule 3b was dead code.
        # v1.10: informational ask -> AUTO, always.
        action = resolve_pricing_action(
            quantity=30, amount=30 * BULK_RATE_INR, intent=INTENT_ASK
        )
        self.assertEqual(action, ACTION_AUTO)

    def test_commit_to_50_trays_escalates(self):
        # "ok I'll take 50" — both rules fire (bulk commit AND ₹37,450 >
        # ₹1,500) and previously resolved by rule order, not intent. Now one
        # deterministic outcome: founder closes every bulk deal.
        action = resolve_pricing_action(
            quantity=50, amount=50 * BULK_RATE_INR, intent=INTENT_COMMIT
        )
        self.assertEqual(action, ACTION_ESCALATE)

    def test_minimum_bulk_boundary_20_trays(self):
        # Exactly 20 trays (₹14,980) is the smallest order where both rules
        # overlap. Ask stays AUTO; commit escalates.
        implied = 20 * BULK_RATE_INR
        self.assertEqual(
            resolve_pricing_action(quantity=20, amount=implied, intent=INTENT_ASK),
            ACTION_AUTO,
        )
        self.assertEqual(
            resolve_pricing_action(quantity=20, amount=implied, intent=INTENT_COMMIT),
            ACTION_ESCALATE,
        )


class HardMoneyThresholdBoundaries(unittest.TestCase):
    """brain.md worked examples: strictly above ₹1,500 escalates, at/below does not."""

    def test_commit_at_exactly_1500_is_auto(self):
        action = resolve_pricing_action(quantity=2, amount=1500, intent=INTENT_COMMIT)
        self.assertEqual(action, ACTION_AUTO)

    def test_commit_at_1501_escalates(self):
        action = resolve_pricing_action(quantity=2, amount=1501, intent=INTENT_COMMIT)
        self.assertEqual(action, ACTION_ESCALATE)

    def test_small_commit_with_unknown_amount_is_auto(self):
        # Below 20 trays with no determinable amount: never guess an
        # escalation — the LLM (with brain.md) still handles nuance.
        action = resolve_pricing_action(quantity=3, amount=None, intent=INTENT_COMMIT)
        self.assertEqual(action, ACTION_AUTO)

    def test_invalid_intent_raises(self):
        with self.assertRaises(ValueError):
            resolve_pricing_action(quantity=5, amount=100, intent="maybe")


class CommitPhraseDetection(unittest.TestCase):
    """The founder-confirmed commit phrases from brain.md Rule 3b-i."""

    def test_ill_take_50(self):
        self.assertEqual(detect_bulk_commit_quantity("ok I'll take 50"), 50)

    def test_lets_do_30(self):
        self.assertEqual(detect_bulk_commit_quantity("let's do 30"), 30)

    def test_how_do_i_pay_for_25(self):
        self.assertEqual(detect_bulk_commit_quantity("how do I pay for 25"), 25)

    def test_curly_apostrophe_variant(self):
        self.assertEqual(detect_bulk_commit_quantity("I’ll take 20 trays"), 20)

    def test_rate_ask_is_not_a_commit(self):
        # The conflict message itself: an ask must NOT register as a commit.
        self.assertIsNone(detect_bulk_commit_quantity("what's the rate for 30 trays?"))

    def test_unrelated_message_is_none(self):
        self.assertIsNone(detect_bulk_commit_quantity("do you have a return policy?"))

    def test_empty_and_none_safe(self):
        self.assertIsNone(detect_bulk_commit_quantity(""))
        self.assertIsNone(detect_bulk_commit_quantity(None))


class PairsAreNotTrays(unittest.TestCase):
    """Audit finding 3: "20 pairs" got the 20+ TRAY bulk quote, and "I'll take
    25 pairs" counted as 25 trays. Pairs convert at 10 per tray; the ₹749 rate
    starts at 20 trays = 200 pairs."""

    def test_ten_pairs_per_tray(self):
        self.assertEqual(PAIRS_PER_TRAY, 10)
        self.assertEqual(BULK_MIN_TRAYS * PAIRS_PER_TRAY, 200)

    def test_quantities_the_brief_lists(self):
        for message, trays in (
            ("im a MUA, need 20 pairs for my bridal kit. whats ur bulk price?", 2),
            ("50 pairs", 5),
            ("200 pairs", 20),
            ("25 trays", 25),
            ("I'll take 25 pairs", 2.5),
            ("need 3 trays", 3),
            ("is 25 trays bulk?", 25),
            ("1 tray", 1),
            ("need 1 pair", 0.1),
        ):
            with self.subTest(message=message):
                self.assertEqual(trays_mentioned(message), trays)

    def test_bulk_rate_only_from_200_pairs(self):
        for message, bulk in (("20 pairs", False), ("50 pairs", False), ("199 pairs", False),
                              ("200 pairs", True), ("25 trays", True), ("need 3 trays", False)):
            with self.subTest(message=message):
                self.assertEqual(trays_mentioned(message) >= BULK_MIN_TRAYS, bulk)

    def test_a_tray_description_is_not_an_order(self):
        self.assertIsNone(trays_mentioned("how many pairs in one tray?"))
        self.assertIsNone(trays_mentioned("is it 10 pairs each?"))
        self.assertIsNone(trays_mentioned("so 10 pairs per tray?"))
        self.assertIsNone(trays_mentioned("GS1 tray price?"))
        self.assertIsNone(trays_mentioned(""))
        self.assertIsNone(trays_mentioned(None))

    def test_several_quantities_add_up(self):
        self.assertEqual(trays_mentioned("5 trays for the salon and 50 pairs for my kit"), 10)

    def test_ill_take_25_pairs_is_not_25_trays(self):
        qty = detect_bulk_commit_quantity("ok I'll take 25 pairs")
        self.assertEqual(qty, 2.5)
        self.assertEqual(resolve_pricing_action(quantity=qty, amount=None, intent=INTENT_COMMIT),
                         ACTION_AUTO)

    def test_ill_take_200_pairs_is_a_20_tray_commit(self):
        qty = detect_bulk_commit_quantity("ok I'll take 200 pairs")
        self.assertEqual(qty, 20)
        self.assertEqual(resolve_pricing_action(quantity=qty, amount=qty * BULK_RATE_INR,
                                                intent=INTENT_COMMIT), ACTION_ESCALATE)

    def test_bare_numbers_and_trays_still_mean_trays(self):
        self.assertEqual(detect_bulk_commit_quantity("ok I'll take 25"), 25)
        self.assertEqual(detect_bulk_commit_quantity("let's do 30 trays"), 30)
        self.assertEqual(detect_bulk_commit_quantity("how do I pay for 20 pairs"), 2)


if __name__ == "__main__":
    unittest.main()
