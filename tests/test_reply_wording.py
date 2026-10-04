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


if __name__ == "__main__":
    unittest.main()
