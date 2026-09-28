"""Brand settings file (multi-brand support, task 1).

  - brands/glamshelf.json is the default and brands/example.json (a FAKE
    brand) loads and validates;
  - the output guard, run with the example brand, allows the example's
    own domain / handle / email / amounts and REJECTS glamshelf.in,
    @glamshelfstore, the Glam Shelf email and Glam Shelf's ₹799;
  - a copy started with BRAND_CONFIG_PATH=brands/example.json uses the
    example's values everywhere (checked in a fresh process, since the
    settings are read once at import);
  - a broken or missing brand file stops startup — it never falls back to
    Glam Shelf's values;
  - "disabled" (null) review requests / Shopify feed really switch off.

The exact Glam Shelf values are pinned by tests/test_brand_golden.py.
No network: sends and fetches are stubbed; subprocesses make no calls
beyond what importing app already does in every test module.

Run:  python -m unittest tests.test_brand_config
"""
import io, json, os, subprocess, sys, tempfile, unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if "app" not in sys.modules:
    os.environ["DB_PATH"] = os.path.join(
        tempfile.mkdtemp(prefix="glamshelf-brand-config-test-"), "test.db"
    )
    os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
    os.environ.pop("BRAND_CONFIG_PATH", None)
    os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
    os.environ.setdefault("DASHBOARD_KEY", "t")
import app as glam
import brand_config
import output_guard

EXAMPLE = ROOT / "brands" / "example.json"
GLAMSHELF = ROOT / "brands" / "glamshelf.json"


MARK = "@@RESULT@@"


def result_json(result: subprocess.CompletedProcess):
    """The JSON the child printed after MARK (app's background threads may
    print around it)."""
    for line in result.stdout.splitlines():
        if line.startswith(MARK):
            return json.loads(line[len(MARK):])
    raise AssertionError(f"no result line in child output:\n{result.stdout[-2000:]}\n{result.stderr[-2000:]}")


def run_python(code: str, **env) -> subprocess.CompletedProcess:
    """Run `code` in a fresh interpreter from the project folder, with the
    current environment (the offline test shim included) plus `env`."""
    full_env = {**os.environ, **env}
    return subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=full_env,
        capture_output=True, text=True, encoding="utf-8", timeout=180,
    )


class LoadTest(unittest.TestCase):
    def test_default_is_glamshelf(self):
        self.assertTrue(brand_config.BRAND_IS_DEFAULT)
        self.assertEqual(brand_config.BRAND_CONFIG_FILE.resolve(), GLAMSHELF.resolve())
        self.assertEqual(brand_config.BRAND["brand_name"], "The Glam Shelf")

    def test_both_committed_files_validate(self):
        for path in (GLAMSHELF, EXAMPLE):
            with self.subTest(path=path.name):
                brand_config.load_brand_config(path)

    def test_relative_path_resolves_against_the_project(self):
        brand = brand_config.load_brand_config("brands/example.json")
        self.assertEqual(Path(brand["_file"]).resolve(), EXAMPLE.resolve())

    def test_example_is_clearly_fake(self):
        brand = brand_config.load_brand_config(EXAMPLE)
        self.assertIn("FAKE", json.loads(EXAMPLE.read_text(encoding="utf-8"))["_comment"])
        self.assertTrue(brand["website_domain"].endswith(".example"))
        self.assertTrue(brand["email"].endswith(".example"))
        brand.pop("_file")
        self.assertNotIn("glam", json.dumps(brand).lower())


class BadFileTest(unittest.TestCase):
    """A broken brand file must stop startup, never fall back to Glam Shelf."""

    def write(self, data) -> Path:
        folder = Path(tempfile.mkdtemp(prefix="brand-config-bad-"))
        path = folder / "brand.json"
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return path

    def example(self) -> dict:
        return json.loads(EXAMPLE.read_text(encoding="utf-8"))

    def assert_rejected(self, data, *words):
        with self.assertRaises(brand_config.BrandConfigError) as ctx:
            brand_config.load_brand_config(self.write(data))
        for word in words:
            self.assertIn(word, str(ctx.exception))

    def test_missing_file(self):
        with self.assertRaises(brand_config.BrandConfigError) as ctx:
            brand_config.load_brand_config(ROOT / "brands" / "no-such-brand.json")
        self.assertIn("not found", str(ctx.exception))

    def test_not_json(self):
        self.assert_rejected("{not json", "unreadable")

    def test_missing_key_is_named(self):
        data = self.example()
        del data["messages"]["lead_reply"]
        self.assert_rejected(data, "messages.lead_reply", "missing")

    def test_unknown_key_is_named(self):
        data = self.example()
        data["messages"]["reveiw_request"] = "typo"
        self.assert_rejected(data, "messages.reveiw_request", "unknown")

    def test_comment_keys_are_ignored(self):
        data = self.example()
        data["pricing"]["_comment"] = "notes are fine"
        brand_config.load_brand_config(self.write(data))

    def test_wrong_types(self):
        for section, key, value, word in (
            ("pricing", "free_shipping_threshold_inr", "999", "positive number"),
            ("pricing", "bulk_min_units", 12.5, "whole number"),
            ("messages", "lead_reply", None, "non-empty text"),
            ("messages", "lead_reply", "   ", "non-empty text"),
            ("allowed_links", "domains", "a.example", "list"),
        ):
            with self.subTest(key=key, value=value):
                data = self.example()
                data[section][key] = value
                self.assert_rejected(data, f"{section}.{key}", word)

    def test_bad_placeholder(self):
        data = self.example()
        data["messages"]["delivered"] = "Hi {firstname}!"
        self.assert_rejected(data, "messages.delivered", "{first_name}")
        data["messages"]["delivered"] = "Hi {first_name}! {"
        self.assert_rejected(data, "messages.delivered")

    def test_short_bot_signature(self):
        data = self.example()
        data["bot_text_signatures"] = ["hi"]
        self.assert_rejected(data, "too short")

    def test_app_refuses_to_start_on_a_missing_file(self):
        result = run_python("import brand_config", BRAND_CONFIG_PATH="brands/no-such-brand.json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("brand settings file not found", result.stderr)


class ExampleBrandGuardTest(unittest.TestCase):
    """Output guard with the example brand (in process, via brand=)."""

    @classmethod
    def setUpClass(cls):
        cls.brand = brand_config.load_brand_config(EXAMPLE)

    def check(self, text, prices=(599,)):
        return output_guard.check_reply(text, prices, brand=self.brand)

    def test_allows_its_own_links(self):
        for text in (
            "Shop at examplecandle.example",
            "See https://www.examplecandle.example/products/amber",
            "Track at https://track.examplecourier.example/123",
            "DM @examplecandleco or instagram.com/examplecandleco",
            "Mail hello@examplecandle.example",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.check(text), [])

    def test_rejects_glamshelf(self):
        for text in (
            "Shop at glamshelf.in",
            "See https://glamshelf.in/pages/reviews",
            "Follow @glamshelfstore",
            "Follow instagram.com/glamshelfstore",
            "Mail glamshelfstore@gmail.com",
        ):
            with self.subTest(text=text):
                reasons = self.check(text)
                self.assertEqual(len(reasons), 1, reasons)
                self.assertTrue(reasons[0].startswith("rule 3"), reasons)

    def test_amounts_are_the_example_brands(self):
        self.assertEqual(self.check("Free shipping above ₹999, bulk ₹399, floor ₹349, ₹49 wick trimmer"), [])
        self.assertEqual(self.check("Orders above ₹2,500 need the team."), [])
        self.assertEqual(self.check("Two jars are ₹1,198."), [])          # 2 x live ₹599
        self.assertTrue(self.check("Free shipping above ₹799")[0].startswith("rule 1"))
        self.assertTrue(self.check("Bulk is ₹749 a tray")[0].startswith("rule 1"))

    def test_glamshelf_default_unchanged(self):
        # The default (no brand=) is still Glam Shelf's rules.
        self.assertEqual(output_guard.check_reply("Shop at glamshelf.in", []), [])
        self.assertTrue(output_guard.check_reply("Shop at examplecandle.example", [])[0].startswith("rule 3"))


class ExampleBrandProcessTest(unittest.TestCase):
    """A copy started with BRAND_CONFIG_PATH=brands/example.json."""

    def test_guard_and_pricing_modules(self):
        result = run_python(
            "import json, output_guard, pricing_rules\n"
            f"print({MARK!r} + json.dumps({{\n"
            "  'glamshelf': output_guard.check_reply('Shop at glamshelf.in', []),\n"
            "  'own': output_guard.check_reply('Shop at examplecandle.example ₹999', []),\n"
            "  'fixed': sorted(output_guard.FIXED_ALLOWED_INR),\n"
            "  'email': output_guard.BRAND_EMAIL,\n"
            "  'pricing': [pricing_rules.BULK_RATE_INR, pricing_rules.BULK_MIN_TRAYS,\n"
            "              pricing_rules.HARD_MONEY_THRESHOLD_INR],\n"
            "  'commit_12': pricing_rules.resolve_pricing_action(12, None, 'commit'),\n"
            "  'commit_11': pricing_rules.resolve_pricing_action(11, None, 'commit'),\n"
            "}))",
            BRAND_CONFIG_PATH="brands/example.json",
        )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        out = result_json(result)
        self.assertEqual(len(out["glamshelf"]), 1)
        self.assertTrue(out["glamshelf"][0].startswith("rule 3"))
        self.assertEqual(out["own"], [])
        self.assertEqual(out["fixed"], [49, 349, 399, 999, 2500])
        self.assertEqual(out["email"], "hello@examplecandle.example")
        self.assertEqual(out["pricing"], [399, 12, 2500])
        self.assertEqual(out["commit_12"], "ESCALATE")
        self.assertEqual(out["commit_11"], "AUTO")

    def test_app_uses_the_example_brand(self):
        with tempfile.TemporaryDirectory(prefix="brand-example-app-") as tmp:
            result = run_python(
                "import io, json, contextlib\n"
                "with contextlib.redirect_stdout(io.StringIO()):\n"
                "    import app\n"
                "    client = app.app.test_client()\n"
                "    login = client.get('/login').get_data(as_text=True)\n"
                "    inventory = app.get_live_inventory()\n"
                f"print({MARK!r} + json.dumps({{\n"
                "  'lead': app.LEAD_REPLY, 'photo': app.INSTAGRAM_PHOTO_REPLY,\n"
                "  'allergy': app.ALLERGY_HOLDING_REPLY,\n"
                "  'escalate': app.ESCALATE_FALLBACK_HOLDING_REPLY,\n"
                "  'review': app.REVIEW_REQUEST_TEMPLATE, 'shopify': app.SHOPIFY_PRODUCTS_URL,\n"
                "  'inventory': inventory, 'policies': app._POLICY_PROMPT_PAGES,\n"
                "  'rag_policies': app._RAG_POLICY_SOURCES,\n"
                "  'signatures': app._BOT_TEXT_SIGNATURES,\n"
                "  'vision': app.VISION_SYSTEM_PROMPT, 'login': login,\n"
                "}))",
                BRAND_CONFIG_PATH="brands/example.json",
                DB_PATH=os.path.join(tmp, "test.db"),
                GITHUB_TOKEN="", GITHUB_REPO="",
                SECRET_KEY="t", APP_PASSWORD="t", DASHBOARD_KEY="t",
            )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        out = result_json(result)
        example = brand_config.load_brand_config(EXAMPLE)
        self.assertEqual(out["lead"], example["messages"]["lead_reply"])
        self.assertEqual(out["photo"], example["messages"]["instagram_photo_reply"])
        self.assertEqual(out["escalate"], example["messages"]["escalate_fallback_holding_reply"])
        self.assertTrue(out["allergy"].startswith(glam.ALLERGY_SAFETY_TEXT + " "))
        self.assertTrue(out["allergy"].endswith(example["messages"]["allergy_holding_line"]))
        self.assertIsNone(out["review"])
        self.assertIsNone(out["shopify"])
        self.assertEqual(out["inventory"], "")
        self.assertEqual(out["policies"], [["Refund Policy", "https://examplecandle.example/policies/refund-policy"]])
        self.assertEqual(out["rag_policies"], [])
        self.assertEqual(out["signatures"], ["track.examplecourier.example", "here's your tracking link"])
        self.assertIn("Example Candle Co, a small-batch candle brand", out["vision"])
        self.assertIn("Amber Woods Jar Candle", out["vision"])
        self.assertIn("Example Candle Co", out["login"])
        self.assertIn("examplecandle.example", out["login"])
        for text in (out["lead"], out["photo"], out["allergy"], out["escalate"], out["vision"], out["login"]):
            self.assertNotIn("glam", text.lower())
        # (The login page's logo SVG carries an "eye / lash mark" HTML comment.)
        for text in (out["lead"], out["photo"], out["allergy"], out["escalate"], out["vision"]):
            self.assertNotIn("lash", text.lower())


class DisabledFeaturesTest(unittest.TestCase):
    def test_review_request_off(self):
        glam._scheduled_reviews.pop("brand-test-order", None)
        with patch.object(glam, "REVIEW_REQUEST_TEMPLATE", None), \
             patch.object(glam.threading, "Timer") as timer, \
             redirect_stdout(io.StringIO()) as buf:
            glam._schedule_review_request("brand-test-order", "#1", "919876543210", "Priya")
        timer.assert_not_called()
        self.assertNotIn("brand-test-order", glam._scheduled_reviews)
        self.assertIn("Review requests are off", buf.getvalue())

    def test_shopify_feed_off(self):
        get = Mock()
        with patch.object(glam, "SHOPIFY_PRODUCTS_URL", None), \
             patch.object(glam.requests, "get", get), \
             patch.object(glam, "_fetch_policy_page_html", lambda url: ""), \
             patch.dict(glam._inventory_cache, {"text": "", "fetched_at": 0.0}), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(glam.get_live_inventory(), "")
            self.assertEqual(glam._rag_build_corpus(), [])
        get.assert_not_called()

    def test_dashboard_calls_its_own_server_without_an_api_base(self):
        client = glam.app.test_client()
        with patch.dict(glam.BRAND, {"dashboard_api_base": None}), redirect_stdout(io.StringIO()):
            page = client.get("/dashboard?key=" + glam.DASHBOARD_KEY).get_data(as_text=True)
        self.assertIn('const API_BASE = "";', page)
        self.assertIn("Could not reach localhost", page)


if __name__ == "__main__":
    unittest.main()
