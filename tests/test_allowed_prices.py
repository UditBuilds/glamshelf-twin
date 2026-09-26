"""The output guard's allowed prices survive a restart during a Shopify
outage.

Every good Shopify inventory fetch saves its price set to
ALLOWED_PRICES_PATH (/var/data/allowed_prices.json on Render's persistent
disk). On startup the app loads that file; if it doesn't exist either, it
falls back to the prices in brain.md's Section 2 product table. Only if all
three are unavailable does the guard allow just the fixed amounts.

No live API call anywhere here: Shopify is stubbed and the file path is a
temp directory.

Run:  python -m unittest tests.test_allowed_prices
"""
import io, json, os, sys, tempfile, unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="glamshelf-allowed-prices-test-"), "test.db"
)
os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
os.environ.setdefault("DASHBOARD_KEY", "t")
import app as glam

# brain.md's product table: Clean Girl, Kawaii, Mink Duo, Everyday + Glam
# Duo, Mink Trio, GS1-GS3.
BRAIN_PRICES = {249.0, 299.0, 499.0, 699.0, 849.0}


def quiet(fn, *a, **k):
    with redirect_stdout(io.StringIO()) as buf:
        result = fn(*a, **k)
    return result, buf.getvalue()


class AllowedPricesTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="glamshelf-prices-")
        self.path = os.path.join(self.dir, "allowed_prices.json")
        for p in (
            patch.object(glam, "ALLOWED_PRICES_PATH", self.path),
            patch.dict(glam._inventory_cache,
                       {"text": "", "fetched_at": 0.0, "prices": set(), "prices_source": ""}),
        ):
            p.start()
            self.addCleanup(p.stop)

    def fetch(self, products):
        resp = Mock(ok=True)
        resp.json.return_value = {"products": products}
        with patch.object(glam.requests, "get", Mock(return_value=resp)):
            quiet(glam.get_live_inventory)

    # ---- saving ----

    def test_good_fetch_saves_the_price_set(self):
        self.fetch([
            {"title": "GS1", "variants": [{"available": True, "price": "849.00"}]},
            {"title": "KAWAII", "variants": [{"available": False, "price": "299.00"}]},
        ])
        with open(self.path, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["prices"], [299.0, 849.0])
        self.assertIn("saved_at", saved)
        self.assertEqual(glam._inventory_cache["prices_source"], "shopify")

    def test_failed_fetch_keeps_the_saved_file(self):
        self.fetch([{"title": "GS1", "variants": [{"available": True, "price": "849.00"}]}])
        with patch.object(glam.requests, "get", Mock(side_effect=glam.requests.ConnectionError())):
            quiet(glam.get_live_inventory)
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["prices"], [849.0])

    def test_missing_folder_is_skipped_not_created(self):
        missing = os.path.join(self.dir, "no-disk", "allowed_prices.json")
        with patch.object(glam, "ALLOWED_PRICES_PATH", missing):
            _, out = quiet(glam._save_allowed_prices, {849.0})
        self.assertFalse(os.path.exists(os.path.dirname(missing)))
        self.assertIn("Not saved", out)

    # ---- fallback 1: the saved file ----

    def test_startup_loads_the_saved_file(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"prices": [249.0, 999.0]}, f)
        (prices, source), _ = quiet(glam._load_startup_prices)
        self.assertEqual((prices, source), ({249.0, 999.0}, "file"))

    def test_saved_file_prices_pass_the_guard_after_a_restart(self):
        # Shopify had a ₹999 price (not in brain.md) before the restart.
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"prices": [999.0]}, f)
        (prices, source), _ = quiet(glam._load_startup_prices)
        glam._inventory_cache["prices"] = prices
        note, _ = quiet(glam._ig_output_guard, "s1", "AUTO", "It's ₹999 🤍")
        self.assertEqual(note, "")

    # ---- fallback 2: brain.md's product table ----

    def test_no_file_falls_back_to_brain_md_prices(self):
        self.assertFalse(os.path.exists(self.path))
        (prices, source), out = quiet(glam._load_startup_prices)
        self.assertEqual((prices, source), (BRAIN_PRICES, "brain.md"))
        self.assertIn("using brain.md", out)

    def test_unreadable_file_falls_back_to_brain_md_prices(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        (prices, source), _ = quiet(glam._load_startup_prices)
        self.assertEqual((prices, source), (BRAIN_PRICES, "brain.md"))

    def test_brain_md_prices_pass_the_guard(self):
        (prices, _), _ = quiet(glam._load_startup_prices)
        glam._inventory_cache["prices"] = prices
        note, _ = quiet(glam._ig_output_guard, "s1", "AUTO", "Clean Girl is ₹249, GS1 is ₹849 🤍")
        self.assertEqual(note, "")

    # ---- nothing at all ----

    def test_no_file_and_no_brain_table_allows_only_fixed_amounts(self):
        with patch.object(glam, "BRAIN_FILE", Path(self.dir) / "missing-brain.md"):
            (prices, source), _ = quiet(glam._load_startup_prices)
        self.assertEqual((prices, source), (set(), ""))
        glam._inventory_cache["prices"] = prices
        note, _ = quiet(glam._ig_output_guard, "s1", "AUTO", "GS1 is ₹849 🤍")
        self.assertIn("rule 1", note)


if __name__ == "__main__":
    unittest.main()
