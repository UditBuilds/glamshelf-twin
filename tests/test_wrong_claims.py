"""Twin doesn't repeat storefront marketing copy as fact (audit findings 2,
5, 9 and 17).

brain.md's Output Contract used to say retrieved facts beat its templates,
so Shopify copy became Twin's claims: "even on sensitive eyes", "without
irritation", Clean Girl's "natural hair fibers". Storefront copy may now
give product specs only (pairs, length, style) — never safety, comfort,
material, band or origin claims — and both injected blocks say so. These
tests pin the brain.md rules and the prompt text.

No live API call anywhere here: Shopify is stubbed.

Run:  python -m unittest tests.test_wrong_claims
"""
import io, os, re, sys, tempfile, unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="glamshelf-wrong-claims-test-"), "test.db"
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


def brain_line(start: str) -> str:
    return next(l for l in BRAIN.splitlines() if l.startswith(start))


def template_after(heading: str) -> str:
    """The first quoted template (> "…") after a brain.md heading."""
    section = BRAIN.split(heading, 1)[1]
    return re.search(r'^> "(.+?)"$', section, re.M).group(1)


def guard(text: str) -> list:
    return output_guard.check_reply(text, LIVE_PRICES)


class StorefrontCopyGivesSpecsOnly(unittest.TestCase):
    """brain.md:115 — retrieved copy overrides brain.md for product specs only."""

    def test_old_rule_is_gone(self):
        self.assertNotIn("Retrieved facts beat blanket templates", BRAIN)
        self.assertNotIn("the specific retrieved fact is correct for that SKU", BRAIN)

    def test_new_rule_names_both_blocks_and_every_protected_topic(self):
        rule = brain_line("- **Storefront copy is marketing — this file wins:**")
        for block in ("`[LIVE INVENTORY]`", "`[RETRIEVED CONTEXT]`"):
            self.assertIn(block, rule)
        self.assertIn("pairs per tray, lash length, style and look", rule)
        for topic in ("safety", "comfort", "sensitive eyes", "irritation",
                      "eye or skin suitability", "materials or fibers", "country of origin"):
            self.assertIn(topic, rule)
        self.assertIn("GS2's band is thicker than GS1's", rule)
        self.assertIn('"Feather-light band"', rule)

    def test_rag_header_keeps_its_prefix_and_limits_product_copy(self):
        h = glam.RAG_CONTEXT_HEADER
        # The audit runner and the parity test look for this exact prefix.
        self.assertTrue(h.startswith("[RETRIEVED CONTEXT]\nThe following is supplementary"))
        self.assertIn("does not override any classification, escalation, or Never-list rule", h)
        for topic in ("marketing copy", "product specs", "safety", "comfort",
                      "sensitive-eye", "material", "origin", "GS2's band is thicker than GS1's"):
            self.assertIn(topic, h)

    def test_inventory_block_says_its_product_text_is_marketing_copy(self):
        resp = Mock(ok=True)
        resp.json.return_value = {"products": [{
            "title": "CLEAN GIRL – Natural Hair Lashes",
            "body_html": "<p>Crafted with fine, lightweight natural hair fibers.</p>",
            "variants": [{"available": True, "price": "249.00", "sku": "CG-NAT-01"}],
        }]}
        with patch.dict(glam._inventory_cache, {"text": "", "fetched_at": 0.0}), \
             patch.object(glam, "_save_allowed_prices", lambda prices: None), \
             patch.object(glam.requests, "get", Mock(return_value=resp)):
            block = quiet(glam.get_live_inventory)
        lines = block.splitlines()
        self.assertEqual(lines[0], "[LIVE INVENTORY - checked now]")
        self.assertEqual(lines[1], glam.INVENTORY_COPY_NOTE)
        self.assertTrue(lines[2].startswith("CLEAN GIRL – Natural Hair Lashes: IN STOCK | SKU CG-NAT-01 | "))
        for topic in ("marketing copy", "product specs", "safety", "comfort",
                      "sensitive eyes", "materials", "band thickness", "country of origin"):
            self.assertIn(topic, glam.INVENTORY_COPY_NOTE)
        # The note must not look like a stock line.
        self.assertNotIn(": IN STOCK", glam.INVENTORY_COPY_NOTE)
        self.assertNotIn(": SOLD OUT", glam.INVENTORY_COPY_NOTE)

    def test_no_usable_products_still_means_no_block(self):
        resp = Mock(ok=True)
        resp.json.return_value = {"products": [{"title": "GS1", "variants": [{"price": "849.00"}]}]}
        with patch.dict(glam._inventory_cache, {"text": "", "fetched_at": 0.0}), \
             patch.object(glam, "_save_allowed_prices", lambda prices: None), \
             patch.object(glam.requests, "get", Mock(return_value=resp)):
            self.assertEqual(quiet(glam.get_live_inventory), "")


class SensitiveEyes(unittest.TestCase):
    """Finding 2: a pre-purchase sensitive-eye question gets no comfort or
    suitability claim — the founder's template, AUTO, no SAFETY tag."""
    HEADING = "**Sensitive eyes (before buying"
    TEMPLATE = (
        "We can't guarantee our lashes will suit sensitive eyes. Before wearing them, "
        "patch-test the lash glue on your inner arm for 24 hours. If you notice any "
        "irritation, remove the lashes and stop using them 🤍"
    )

    def test_template_is_the_founders_wording(self):
        self.assertEqual(template_after(self.HEADING), self.TEMPLATE)

    def test_template_makes_no_comfort_or_suitability_claim(self):
        low = self.TEMPLATE.lower()
        for word in ("comfortable", "gentle", "lightweight", "feather", "safe", "most customers"):
            self.assertNotIn(word, low)
        for must in ("can't guarantee", "inner arm", "24 hours", "remove the lashes", "stop using"):
            self.assertIn(must, low)

    def test_template_passes_the_output_guard(self):
        self.assertEqual(guard(self.TEMPLATE), [])

    def test_it_is_auto_without_the_safety_tag(self):
        heading = brain_line(self.HEADING)
        self.assertIn('🟢 AUTO, tag ""', heading)
        self.assertIn('not "SAFETY"', heading)
        row = brain_line("| 58 | Sensitive eyes, before buying")
        self.assertIn('🟢 AUTO, tag ""', row)
        self.assertIn("Rule 32", row)

    def test_a_reaction_still_escalates(self):
        section = BRAIN.split(self.HEADING, 1)[1].split("**Lash extension service request", 1)[0]
        self.assertIn('allergic reaction escalation (🔴 ESCALATE, tag "SAFETY")', section)
        self.assertIn("| 32 | Allergic reaction claim | 🔴 ESCALATE", BRAIN)

    def test_never_26_points_at_the_template(self):
        never = brain_line("26. **NEVER** make medical")
        self.assertIn("comfort or suitability claims", never)
        self.assertIn("Sensitive eyes template", never)
        self.assertNotIn('redirect to *"please patch-test first 🤍"*', never)


class CleanGirlIsSynthetic(unittest.TestCase):
    """Finding 5: Clean Girl is synthetic (founder fact). Twin says so and
    never explains away the storefront's "natural hair" wording."""
    HEADING = '**Clean Girl — "natural hair", real hair or vegan question:**'
    EXPLAINING = re.compile(r"refers? to|describes|means|just the name|in the name", re.I)

    def test_auto_template_says_synthetic_and_explains_nothing(self):
        self.assertIn("🟢 AUTO", brain_line(self.HEADING))
        t = template_after(self.HEADING)
        self.assertEqual(t, "Clean Girl is made with synthetic fibers — no real or animal hair "
                            "— and our whole range is 100% vegan and cruelty-free 🤍")
        self.assertIsNone(self.EXPLAINING.search(t))
        self.assertEqual(guard(t), [])

    def test_the_rule_forbids_explaining_the_wording(self):
        section = BRAIN.split(self.HEADING, 1)[1].split("**Sourcing / manufacturing question", 1)[0]
        self.assertIn('say nothing about the "natural hair" wording', section)
        self.assertIn("never explain what it means or describes", section)

    def test_asking_again_goes_to_a_draft(self):
        section = BRAIN.split(self.HEADING, 1)[1].split("**Sourcing / manufacturing question", 1)[0]
        self.assertIn("asks again why the website says natural hair → 🟡 DRAFT+APPROVE", section)
        second = re.findall(r'^> "(.+?)"$', section, re.M)[1]
        self.assertIn("synthetic fibers, not real hair", second)
        self.assertIsNone(self.EXPLAINING.search(second))

    def test_product_table_says_synthetic(self):
        row = brain_line("| CLEAN GIRL — Natural Hair Lashes | ₹249 |")
        self.assertIn("Synthetic fibers — no real or animal hair", row)


if __name__ == "__main__":
    unittest.main()
