"""Reply wording follow-ups from the live tests of 4 Oct 2026.

"how long does a refund take?" got the timings plus "If you've already
emailed us, the team will update you here on the status". Output guard
rule 7 held it, correctly: Twin can't see refunds, and nobody is told to
follow up. The cause was the conversation history. In the verification
runs the same question with an open, angry complaint earlier in the thread
was treated as a question about the customer's own refund (2 of 2), while
with only an answered damage complaint it got the plain timings (4 of 4).
brain.md now has a template for the general question: give the timings
and stop, whatever the history shows.

"which lash is best for a wedding?" got stronger claims than brain.md
makes; see WeddingClaimsStayAsBrainSaysThem.

No live API call anywhere here.

Run:  python -m unittest tests.test_reply_wording
"""
import re, sys, unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import output_guard

BRAIN = (REPO / "brain" / "brain.md").read_text(encoding="utf-8")
LIVE_PRICES = {249.0, 299.0, 499.0, 699.0, 849.0}


def guard(text):
    return output_guard.check_reply(text, LIVE_PRICES)


def template_after(heading):
    return re.search(r'^> "(.+?)"$', BRAIN.split(heading, 1)[1], re.M).group(1)


class RefundTimingGetsTheTimingsAndStops(unittest.TestCase):
    HEADING = '**"How long does a refund take?"**'

    def test_template_is_auto_and_sent(self):
        self.assertIn("🟢 AUTO", next(l for l in BRAIN.splitlines() if l.startswith(self.HEADING)))
        t = template_after(self.HEADING)
        self.assertEqual(t, "Refunds are initiated within 24–48 hours of approval, then reach UPI/bank "
                            "accounts in 5–7 working days and cards in 7–10 working days 🤍")
        self.assertEqual(guard(t), [])

    def test_nothing_after_the_timings_whatever_the_history(self):
        rule = BRAIN.split(self.HEADING, 1)[1].split("### 3.5 Cancellations", 1)[0]
        self.assertIn("Give the timings and stop.", rule)
        self.assertIn('no "the team will update you on the status"', rule)
        self.assertIn("even when the history shows an earlier complaint, a damaged item or an escalation", rule)
        # Their own refund is still the refund complaint rule.
        self.assertIn('"where is my refund"', rule)
        self.assertIn("🔴 ESCALATE", rule)

    def test_ongoing_resolution_doesnt_claim_it(self):
        rule = BRAIN.split("**RULE: ONGOING RESOLUTION CONTEXT**", 1)[1].split("THEN:", 1)[0]
        self.assertIn('"how long does a refund take?" — is not a check-in on their issue', rule)

    def test_the_live_reply_is_still_held(self):
        # The guard was right; it stays as it is.
        held = guard("Refunds are initiated within 24–48 hours of approval, then reach UPI/bank accounts "
                     "in 5–7 working days and cards in 7–10 working days. If you've already emailed us, "
                     "the team will update you here on the status 🤍")
        self.assertTrue(any(r.startswith("rule 7") for r in held), held)


class WeddingClaimsStayAsBrainSaysThem(unittest.TestCase):
    """"which lash is best for a wedding?" (live test, 4 Oct 2026) got "what
    most MUAs choose" and "all-day hold under heavy makeup". brain.md says
    "what MUAs typically choose" and "professional-grade hold and longer
    wear"; "all-day wear" and "under heavy makeup" are in two other
    templates. The reply now has its own template in brain.md's words, and
    the Output Contract forbids stronger or merged claims."""

    HEADING = "**Best lash for a wedding / bridal look (no eye shape given):**"
    GS2 = ("bolder, fuller, and bridal-ready — what MUAs typically choose for wedding-day and event "
           "makeup, with a thicker band built for professional-grade hold and longer wear")

    def test_template_uses_the_gs1_vs_gs2_wording(self):
        self.assertIn("🟢 AUTO", next(l for l in BRAIN.splitlines() if l.startswith(self.HEADING)))
        t = template_after(self.HEADING)
        self.assertEqual(t, f"For a wedding, GS2 is our pick — {self.GS2} 🤍")
        self.assertIn(f"GS2 is {self.GS2} 🤍", template_after("**GS1 vs GS2:**"))
        self.assertEqual(guard(t), [])

    def test_output_contract_forbids_stronger_or_merged_claims(self):
        rule = next(l for l in BRAIN.splitlines()
                    if l.startswith("- **Claims in this file's words, never stronger:**"))
        for phrase in ('"what MUAs typically choose" never becomes "what most MUAs choose"',
                       '"professional-grade hold and longer wear" never becomes "all-day hold"',
                       'is not "all-day hold under heavy makeup"'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, rule)
        # It sits in the Output Contract, which is checked on every reply.
        contract = BRAIN.split("### Output Contract", 1)[1].split("\n### ", 1)[0]
        self.assertIn(rule, contract)

    def test_no_template_makes_the_stronger_claims(self):
        for q in re.findall(r'^> "(.+?)"$', BRAIN, re.M):
            with self.subTest(q=q[:60]):
                self.assertNotRegex(q, r"(?i)\bmost MUAs\b|\ball[- ]day hold\b|\bwon't budge\b")


if __name__ == "__main__":
    unittest.main()
