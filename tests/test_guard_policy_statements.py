"""Output guard: honest policy statements are sent, false promises stay
held (audit finding 7).

Rule 2 used to hold any AUTO reply with code / coupon / promo / discount /
refund / cashback / free anywhere in it, apart from a few phrases that were
cut out wherever they appeared. So honest answers ("no coupon codes", the
refund timeline, "shipping's free since it's above ₹799") were held, while
"free shipping on any order" was sent. Now each sentence is judged on its
own, clause by clause: a clause with one of those words passes only when it
is one of brain.md's approved policy statements.

The texts below are the brief's required cases, the store's refund policy
page, and real model drafts from the audit, PR #46's verification and this
fix's verification runs (quoted, since those files aren't in the repo).

No live API call anywhere here.

Run:  python -m unittest tests.test_guard_policy_statements
"""
import sys, unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import output_guard

BRAIN = (REPO / "brain" / "brain.md").read_text(encoding="utf-8")
LIVE_PRICES = {249.0, 299.0, 499.0, 699.0, 849.0}


def rule2(text):
    return [r for r in output_guard.check_reply(text, LIVE_PRICES) if r.startswith("rule 2")]


class HonestPolicyStatementsAreSent(unittest.TestCase):
    def assertSent(self, texts):
        for text in texts:
            with self.subTest(text=text):
                self.assertEqual(output_guard.check_reply(text, LIVE_PRICES), [])

    def test_brief_required_cases(self):
        self.assertSent([
            # brain.md:399, the return-policy template
            "We accept returns within 14 days of delivery as long as the lashes are unused and in "
            "their original packaging. Just email glamshelfstore@gmail.com with your order ID and "
            "we'll get it started — return shipping is on you unless the item arrived damaged or incorrect 🤍",
            # the refund timeline, brain.md:442-445
            "Once a return is approved, the refund is initiated within 24–48 hours and reaches your "
            "UPI/bank account in 5–7 working days, or your card in 7–10 working days 🤍",
            # brain.md:606, the no-discount answer, and the no-coupon answer
            "Our prices are already reduced from the original MRP — there's no additional discount "
            "available at the moment. Free shipping does apply on orders above ₹799 though 🤍",
            "Our prices are already reduced from the original MRP — there's no additional discount "
            "available at the moment, and no active coupon codes. Free shipping does apply on orders "
            "above ₹799 though 🤍",
            "GS1 is ₹849 for a 10-pair tray — free shipping since it's above ₹799",
            "shipping's free on orders above ₹799",
        ])

    def test_refund_timeline_phrasings(self):
        self.assertSent([
            "Refunds are initiated within 24–48 hours of approval and take 5–7 working days to reach "
            "your original payment method 🤍",
            "Once approved, refunds are initiated within 24–48 hours 🤍",
            "Refunds take 5–7 business days for UPI/bank and 7–10 business days for cards 🤍",
            "Refunds take 7–10 working days for cards 🤍",
            "Approved refunds are initiated within 24–48 hours, then reach UPI/bank in 5–7 working days "
            "and cards in 7–10 working days 🤍",
        ])

    def test_store_refund_policy_page_sentences(self):
        # glamshelf.in/policies/refund-policy, updated 29.09.26
        self.assertSent([
            "If approved: Your refund is initiated within 24–48 hours of approval.",
            "It reaches UPI/bank accounts in 5–7 working days and cards in 7–10 working days.",
            "Shipping charges (if paid) are non-refundable.",
            "Shipping charges, if paid, aren't refundable.",
        ])

    def test_real_drafts_that_used_to_be_held(self):
        self.assertSent([
            # audit B3, main run and both re-runs: "koi coupon code hai? first order pe discount milega?"
            "Our prices are already reduced from the original MRP — there's no additional discount "
            "available at the moment, and no active coupon codes. Free shipping does apply on orders "
            "above ₹799 though 🤍",
            "Our prices are already reduced from the original MRP — there's no additional discount at "
            "the moment, and no active coupon codes right now. Free shipping does apply on orders above "
            "₹799 though 🤍",
            "Our prices are already reduced from the original MRP — there are no coupon codes or "
            "additional first-order discounts at the moment. Free shipping does apply on orders above "
            "₹799 though 🤍",
            # PR #46 verification, P2 run 2
            "GS1 is our soft, natural everyday tray — 10 pairs for ₹849, and shipping's free since it's "
            "above ₹799. You can order it here: glamshelf.in/products/gs1-luxe-light-lash-tray 🤍",
            # BEFORE runs: "how long does a refund take?"
            "Refunds are initiated within 24–48 hours of approval, then take 5–7 working days to reflect "
            "for UPI/bank and 7–10 working days for cards. If you share your order ID, I'll pass it to "
            "the team so they can check yours 🤍",
            "Refunds are initiated within 24–48 hours of approval — from there it reflects in UPI/bank "
            "accounts in 5–7 working days and cards in 7–10 working days 🤍",
            "Refunds are initiated within 24–48 hours of approval, and it then reflects in your bank or "
            "UPI in 5–7 working days, or on card in 7–10 working days. If you share your order ID, I'll "
            "pass it to the team so they can check the status for you 🤍",
            # BEFORE runs: "koi coupon code hai?"
            "No coupon codes running at the moment — our prices are already reduced from MRP. Free "
            "shipping does apply on orders above ₹799 though 🤍",
            "No coupon codes running at the moment — our prices are already reduced, and shipping is "
            "free on orders above ₹799 🤍",
            "No coupon codes running at the moment — our prices are already reduced from MRP, and free "
            "shipping applies on orders above ₹799 🤍",
        ])

    def test_first_after_run_phrasings(self):
        self.assertSent([
            # refund timing and the tester's refund question
            "Sure thing — refunds are initiated within 24–48 hours once a return is received and "
            "approved, and reach UPI or bank accounts in 5–7 working days. Email glamshelfstore@gmail.com "
            "with your order ID and we'll get it started 🤍",
            "We accept returns within 14 days of delivery, as long as the lashes are unused and in their "
            "original packaging — refunds are initiated within 24–48 hours once we receive and approve "
            "the return. Just email glamshelfstore@gmail.com with your order ID and the team will take it "
            "from there 🤍",
            "Once a refund is approved, we initiate it within 24–48 hours — it reaches UPI/bank accounts "
            "in 5–7 working days and cards in 7–10 working days. If you share your order ID, I'll pass it "
            "to the team to check on yours 🤍",
            "Once your refund is approved, it's initiated within 24–48 hours — it reaches UPI/bank "
            "accounts in 5–7 working days and cards in 7–10 working days. Shipping charges, if paid, "
            "aren't refundable 🤍",
            # the free-shipping threshold, said the other way round (brain.md:923)
            "Since Kawaii is ₹299, it's under our ₹799 free-shipping threshold — the exact delivery "
            "charge will show at checkout 🤍",
        ])

    def test_replies_the_old_guard_sent_are_still_sent(self):
        self.assertSent([
            # this fix's first AFTER run, 2-tray discount: the old guard sent it, the first
            # version of this fix held it
            "Our prices are already reduced from the original MRP, so there's no additional discount on "
            "retail orders — but free shipping applies on orders above ₹799, which two trays would "
            "qualify for 🤍",
            # BEFORE runs: "can i get a discount if i buy 2 trays?"
            "Our prices are already reduced from the original MRP — there's no additional discount "
            "available at the moment. Bulk pricing at ₹749/tray applies to orders of 20+ trays, and "
            "free shipping does apply on orders above ₹799 🤍",
            "Our tray prices are already reduced from the original MRP, so there's no additional "
            "discount available — but free shipping applies on orders above ₹799, and trays are ₹849 each 🤍",
            # audit A1, D2, E4 and G1 turn 2 (the sentences with a trigger word)
            "Yes, GS3 is in stock — it's our bestseller half lash tray (10 pairs) at ₹849, with free "
            "shipping since it's above ₹799.",
            "Our bulk rate is ₹749/tray for orders of 20+ trays — but for just 3 trays, regular pricing "
            "of ₹849/tray applies. Since your total is above ₹799, shipping is free.",
            "Happy to help you order GS1 — we're prepaid only, so checkout on glamshelf.in is the way to "
            "go. It's ₹849 and ships free since it's above ₹799 🤍",
            "GS2 is ₹849 for a tray of 10 pairs — that works out to about ₹85 per pair, and each pair is "
            "reusable 5–7 times. Free shipping applies since it's above ₹799",
        ])


class FalsePromisesStayHeld(unittest.TestCase):
    def assertHeld(self, texts):
        for text in texts:
            with self.subTest(text=text):
                self.assertTrue(rule2(text), text)

    def test_brief_required_cases(self):
        self.assertHeld([
            "I'll refund you right away",
            "we'll give you a full refund of ₹849",
            "use code GLAM20 for 20% off",
            "I can give you a discount",
            "free shipping on any order",
            "you'll get a free pair",
            "refunds are processed within 7 business days",
            "your refund will reach you in 24 hours",
        ])

    def test_an_approved_sentence_does_not_cover_another_one(self):
        self.assertEqual(rule2("Free shipping above ₹799. Use code GLAM20 for 20% off 🤍"),
                         ["rule 2 (code/discount/refund/free words): code"])
        self.assertTrue(rule2("Free shipping does apply on orders above ₹799, and you'll get a free pair 🤍"))
        self.assertTrue(rule2("There are no coupon codes right now. I'll refund you right away 🤍"))
        self.assertTrue(rule2("no coupon code needed — you get 20% off automatically"))

    def test_free_shipping_needs_the_threshold_in_the_same_sentence(self):
        self.assertHeld([
            "Add a set, it ships free 🤍",
            "If you add a set, it ships free 🤍",
            "Free shipping on orders above ₹799. Add a set and it ships free too 🤍",
            # audit G2 turn 3, the finding 12 case
            "Kawaii is ₹299, so shipping would fall under the ₹799 free-shipping threshold — the exact "
            "charge shows at checkout. If you add a tray or a set, it ships free 🤍",
            # first AFTER run: honest, but no threshold in the sentence (decision 2)
            "Hey! GS1 Luxe Light Lash Tray is ₹849 for 10 pairs — soft, natural finish, great for "
            "everyday and light bridal looks. You can order it here → "
            "glamshelf.in/products/gs1-luxe-light-lash-tray, and shipping's free on this one 🤍",
            "Shipping cost is calculated and shown at checkout — Kawaii at ₹299 alone falls just under "
            "our free shipping threshold of ₹799. If you'd like, I can suggest a combo that ships free 🤍",
        ])

    def test_free_shipping_claim_with_wrong_maths_stays_held(self):
        # The claim says the order is above ₹799; the amounts say it isn't.
        self.assertHeld([
            "Kawaii (₹299) plus the Mink Duo (₹499) comes to ₹798, and shipping's free since it's above ₹799 🤍",
        ])

    def test_wrong_or_promised_refund_timelines_stay_held(self):
        self.assertHeld([
            # PR #46 verification F7 drafts (the store page's old timeline)
            "Happy to help! Returns are accepted within 14 days of delivery on unused lashes in original "
            "packaging — just email glamshelfstore@gmail.com with your order ID. Refunds are processed "
            "within 7 business days of us receiving the item, back to your original payment method 🤍",
            "We accept returns within 14 days of delivery as long as the lashes are unused and in their "
            "original packaging, and refunds are processed within 7 business days of us receiving the "
            "item back. Just email glamshelfstore@gmail.com with your order ID to get it started 🤍",
            "We accept returns within 14 days of delivery as long as the lashes are unused and in their "
            "original packaging — just email glamshelfstore@gmail.com with your order ID and we'll take "
            "it from there. Refunds are processed within 7 business days once we receive and inspect the item 🤍",
            # the right timeline followed by a wrong one in the same sentence
            "Once approved, refunds are initiated within 24–48 hours, and processed within 7 business days 🤍",
            "Refunds are initiated within 24–48 hours of approval and reach you instantly 🤍",
            "Refunds are initiated within 24–48 hours once a return is approved, and reach you in "
            "7 business days 🤍",
            # no approval condition: a promise to this customer (brain.md:878), not the policy
            "Your refund will be initiated within 24–48 hours 🤍",
            "We'll initiate your refund within 24–48 hours 🤍",
            "Shipping charges (if paid) are refundable 🤍",
            # returns start by email (brain.md:396), not in the DM
            "Great to have you testing it! Refunds are handled by our team: approved refunds are "
            "initiated within 24–48 hours, then reach UPI/bank in 5–7 working days and cards in 7–10 "
            "working days. Any refund request just needs your order ID shared here, and I'll pass it to "
            "the team 🤍",
        ])

    def test_brain_refund_promise_templates_stay_held(self):
        # Customer-specific promises, only ever drafted for approval (brain.md:138).
        for needle in ("your full refund will be initiated", "refund you within 24–48 hours",
                       "The refund is being processed once the pickup is done"):
            line = next(l for l in BRAIN.splitlines() if needle in l)
            with self.subTest(line=line):
                self.assertTrue(rule2(line))

    def test_trigger_words_are_unchanged(self):
        self.assertEqual(
            output_guard._PROMO_WORDS_RE.pattern,
            r"\b(codes?|coupons?|promos?|promo[- ]?codes?|discount\w*|refund\w*|cashbacks?|free\w*)\b")


class BrainTemplates(unittest.TestCase):
    def test_only_the_three_refund_promise_templates_are_held(self):
        held = [line.strip() for line in BRAIN.splitlines()
                if line.lstrip().startswith(">") and rule2(line.lstrip()[1:].strip().strip('"'))]
        self.assertEqual(len(held), 3, held)

    def test_shipping_charges_line_is_in_the_refund_notes(self):
        notes = BRAIN.split("**Refund timeline (when approved):**", 1)[1].split("###", 1)[0]
        self.assertIn("- Shipping charges (if paid) are non-refundable", notes)
        self.assertIn("- Card reflection: 7–10 working days", notes)


if __name__ == "__main__":
    unittest.main()
