"""Pairs are converted to trays before any bulk quote (audit findings 3 and
12).

"im a MUA, need 20 pairs for my bridal kit" got the ₹749 bulk rate, which
starts at 20 TRAYS (200 pairs); 20 pairs is 2 trays at the listed price. The
escalation prefilter also read "I'll take 25 pairs" as 25 trays. And Twin
suggested product combinations to reach free shipping, with wrong maths.

No live API call anywhere here.

Run:  python -m unittest tests.test_pairs_vs_trays
"""
import io, os, re, sys, tempfile, unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="glamshelf-pairs-trays-test-"), "test.db"
)
os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
os.environ.setdefault("DASHBOARD_KEY", "t")
import app as glam
import output_guard

BRAIN = (REPO / "brain" / "brain.md").read_text(encoding="utf-8")
LIVE_PRICES = {249.0, 299.0, 499.0, 699.0, 849.0}


def quiet(fn, *a, **k):
    with redirect_stdout(io.StringIO()):
        return fn(*a, **k)


class PrefilterCountsPairsAsPairs(unittest.TestCase):
    def test_25_pairs_no_longer_escalates_as_25_trays(self):
        # The audit's probe P7 returned 25 here.
        self.assertIsNone(quiet(glam._bulk_commit_prefilter_hit, "ok I'll take 25 pairs"))

    def test_200_pairs_is_a_bulk_commit(self):
        self.assertEqual(quiet(glam._bulk_commit_prefilter_hit, "ok I'll take 200 pairs"), 20)

    def test_trays_still_escalate(self):
        self.assertEqual(quiet(glam._bulk_commit_prefilter_hit, "ok I'll take 25"), 25)
        self.assertEqual(quiet(glam._bulk_commit_prefilter_hit, "let's do 30 trays"), 30)


if __name__ == "__main__":
    unittest.main()
