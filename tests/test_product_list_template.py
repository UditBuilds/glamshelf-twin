"""The product-list reply template in brain.md (PR C2).

The price-list / full-range template is what the model copies for "pp",
"price?" and "show me everything" — read on a phone, often on Instagram.
It must stay: normal-capital product names (not Shopify's CLEAN GIRL),
one product per "•" line with no "|" or ";" packing, under the 900-char
Instagram split threshold, and exactly one 🤍.

Reads brain/brain.md directly; no app import, no network.

Run:  python -m unittest tests.test_product_list_template
"""
import re, unittest
from pathlib import Path

BRAIN = Path(__file__).resolve().parent.parent / "brain" / "brain.md"
HEADING = "**Default product list reply (in-stock only):**"
POLICY_HEADING = "**If the customer also asked about policies**"

EXPECTED = """Singles (1 pair)
• Clean Girl — ₹249 · soft, natural everyday look
• Kawaii — ₹299 · fluffy faux mink volume

Sets
• Mink Duo — ₹499 · 2 pairs of Kawaii
• Everyday + Glam Duo — ₹499 · 1 natural + 1 glam
• Mink Trio — ₹699 · 3 faux mink pairs

Trays (10 pairs)
• GS1 — ₹849 · light, everyday
• GS2 — ₹849 · thicker band, bridal & events
• GS3 — ₹849 · half lashes, natural lift

Free shipping above ₹799 🤍
Which occasion is it for? I'll pick the right one."""

POLICY_LINE = "Happy to explain shipping, returns or lash care — just ask 🤍"


def quoted_block_after(heading: str) -> str:
    """The first "> ..." quoted example after `heading`, without the
    "> " prefixes and the surrounding quote marks."""
    text = BRAIN.read_text(encoding="utf-8")
    start = text.index(heading)
    lines, inside = [], False
    for line in text[start:].splitlines():
        if not inside:
            if line.startswith('> "'):
                inside = True
            else:
                continue
        if not line.startswith(">"):
            break
        lines.append(line[2:] if line.startswith("> ") else "")
    block = "\n".join(lines)
    assert block.startswith('"') and block.endswith('"'), block[-40:]
    return block[1:-1]


class ProductListTemplateTest(unittest.TestCase):
    def setUp(self):
        self.template = quoted_block_after(HEADING)

    def test_template_is_the_agreed_layout(self):
        self.assertEqual(self.template, EXPECTED)

    def test_no_all_caps_product_names(self):
        # Any run of 3+ capital letters, other than the GS1/GS2/GS3 SKUs.
        caps = [w for w in re.findall(r"\b[A-Z][A-Z+ ]*[A-Z]{2,}\b", self.template)
                if not re.fullmatch(r"GS\d", w.strip())]
        self.assertEqual(caps, [])
        for name in ("CLEAN GIRL", "KAWAII", "MINK DUO", "MINK TRIO", "EVERYDAY + GLAM DUO"):
            self.assertNotIn(name, self.template)

    def test_no_pipes_or_semicolons(self):
        self.assertNotIn("|", self.template)
        self.assertNotIn(";", self.template)

    def test_under_900_characters(self):
        # 900 = the point where the Instagram sender starts splitting.
        self.assertLess(len(self.template), 900)

    def test_one_product_per_bullet_line(self):
        bullets = [l for l in self.template.splitlines() if l.startswith("• ")]
        self.assertEqual(len(bullets), 8)
        for line in bullets:
            self.assertEqual(line.count("₹"), 1, line)

    def test_exactly_one_heart(self):
        self.assertEqual(self.template.count("🤍"), 1)

    def test_policy_variant_moves_the_heart_to_the_policy_line(self):
        tail = quoted_block_after(POLICY_HEADING)
        self.assertTrue(tail.endswith(POLICY_LINE))
        self.assertEqual(tail.count("🤍"), 1)
        self.assertIn("Free shipping above ₹799\n", tail)


class WhatsNewTemplateTest(unittest.TestCase):
    """The "What's new?" reply: bullets naming GS3 and the Everyday + Glam
    Duo, ending with a one-line question — no paragraph list."""

    def setUp(self):
        self.reply = quoted_block_after('**"What\'s new? / Any new launches?":**')

    def test_names_gs3_and_the_duo_as_bullets(self):
        bullets = [l for l in self.reply.splitlines() if l.startswith("• ")]
        self.assertEqual(bullets, [
            "• GS3 — ₹849 · half lash tray, natural lift",
            "• Everyday + Glam Duo — ₹499 · 1 natural + 1 glam",
        ])

    def test_ends_with_a_one_line_question(self):
        last = self.reply.splitlines()[-1]
        self.assertTrue(last.endswith("? 🤍"), last)
        self.assertEqual(self.reply.count("?"), 1)
        self.assertEqual(self.reply.count("🤍"), 1)

    def test_no_paragraph_list(self):
        self.assertNotIn(";", self.reply)
        self.assertNotIn("|", self.reply)
        self.assertNotIn("Clean Girl, Kawaii", self.reply)

    def test_passes_the_output_guard(self):
        import sys
        sys.path.insert(0, str(BRAIN.parent.parent))
        import output_guard
        self.assertEqual(output_guard.check_reply(self.reply, {499.0, 849.0}), [])


if __name__ == "__main__":
    unittest.main()
