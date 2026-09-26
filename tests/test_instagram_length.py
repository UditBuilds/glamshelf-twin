"""Instagram reply length cap (audit T2-10).

Meta rejects an Instagram DM text over its limit (treated as 1000
characters) and the customer gets nothing. Now, before sending:
  - a reply of up to 900 characters goes out unchanged, as one message;
  - a longer one is split at a line break or sentence end into at most 2
    messages of up to 900 characters each;
  - if it still doesn't fit, the 2nd message is cut at its last full
    sentence and ends with "Want more details on any of these? 🤍";
  - never mid-word or mid-price, and a split or cut is logged.

No live API call anywhere here: the Graph API send is stubbed.

Run:  python -m unittest tests.test_instagram_length
"""
import io, os, sys, tempfile, unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="glamshelf-ig-length-test-"), "test.db"
)
os.environ["GITHUB_TOKEN"] = ""; os.environ["GITHUB_REPO"] = ""
os.environ.setdefault("SECRET_KEY", "t"); os.environ.setdefault("APP_PASSWORD", "t")
os.environ.setdefault("DASHBOARD_KEY", "t")
import app as glam

SENDER = "17800000000000654"
ENDING = glam.INSTAGRAM_CUT_ENDING
SENTENCE_ENDS = (".", "!", "?", "…", "🤍")


def reply_of(length: int) -> str:
    """A realistic reply of about `length` chars: priced sentences, one
    line break every few sentences, ending with the brain's 🤍."""
    sentences, n = [], 0
    while sum(len(s) + 1 for s in sentences) < length:
        n += 1
        sep = "\n" if n % 4 == 0 else " "
        sentences.append(f"Tray option {n} is ₹849 for ten reusable pairs, soft and light.{sep}")
    return "".join(sentences).strip() + " 🤍"


def words(text: str) -> list[str]:
    return text.split()


class SplitReplyTest(unittest.TestCase):
    def test_short_reply_is_unchanged(self):
        text = "GS1 is ₹849 for 10 pairs — soft and natural 🤍"
        self.assertEqual(glam._ig_split_reply(text), ([text], ""))

    def test_reply_at_exactly_900_chars_is_unchanged(self):
        text = "a" * 899 + "."
        self.assertEqual(glam._ig_split_reply(text), ([text], ""))

    def test_1500_char_reply_goes_out_as_two_parts_with_nothing_dropped(self):
        text = reply_of(1500)
        self.assertGreaterEqual(len(text), 1500)
        parts, action = glam._ig_split_reply(text)
        self.assertEqual(action, "split")
        self.assertEqual(len(parts), 2)
        for part in parts:
            self.assertLessEqual(len(part), 900)
        # Every word arrives, in order — nothing cut mid-word or dropped.
        self.assertEqual(words(parts[0]) + words(parts[1]), words(text))
        # The split falls on a sentence end or line break.
        self.assertTrue(parts[0].endswith(SENTENCE_ENDS), parts[0][-40:])

    def test_3000_char_reply_is_two_parts_cut_at_a_sentence_with_the_ending(self):
        text = reply_of(3000)
        self.assertGreaterEqual(len(text), 3000)
        parts, action = glam._ig_split_reply(text)
        self.assertEqual(action, "cut")
        self.assertEqual(len(parts), 2)
        for part in parts:
            self.assertLessEqual(len(part), 900)
        self.assertTrue(parts[1].endswith("\n\n" + ENDING))
        kept = parts[1][: -len("\n\n" + ENDING)]
        # Both parts end on a full sentence, and together they are the
        # start of the original reply word for word.
        self.assertTrue(parts[0].endswith(SENTENCE_ENDS), parts[0][-40:])
        self.assertTrue(kept.endswith(SENTENCE_ENDS), kept[-40:])
        sent_words = words(parts[0]) + words(kept)
        self.assertEqual(sent_words, words(text)[: len(sent_words)])

    def test_never_splits_a_price_from_its_currency_marker(self):
        # No sentence end or line break anywhere, and the price straddles
        # the 900-char limit: the only space inside it is at index 897,
        # between the marker and "849" (which ends past the limit).
        for marker in ("Rs.", "₹", "INR"):
            with self.subTest(marker=marker):
                pad = 896 - len(marker)
                text = "x" * pad + f" {marker} 849 " + "more words " * 20
                self.assertEqual(text[897], " ")
                parts, _ = glam._ig_split_reply(text)
                self.assertEqual(parts[0], "x" * pad)
                self.assertTrue(parts[1].startswith(f"{marker} 849"))

    def test_rs_dot_before_a_price_is_not_a_sentence_end(self):
        # "Rs." ends at index 897, inside the limit — if it counted as a
        # sentence end the split would land there. It must fall back to
        # the real sentence end, "Hi.".
        text = "Hi. " + ("so soft " * 120)[:889] + " Rs. 849 each. " + "tail words " * 10
        self.assertEqual(text[894:897], "Rs.")
        parts, _ = glam._ig_split_reply(text)
        self.assertEqual(parts[0], "Hi.")

    def test_no_punctuation_falls_back_to_a_word_boundary(self):
        text = " ".join(["lashes"] * 300)  # ~2,100 chars, no sentence end
        parts, action = glam._ig_split_reply(text)
        self.assertEqual(action, "cut")
        for part in parts:
            self.assertLessEqual(len(part), 900)
        body = parts[0] + " " + parts[1][: -len("\n\n" + ENDING)]
        self.assertEqual(set(words(body)), {"lashes"})


class SendInstagramReplyTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.results = []
        p = patch.object(glam, "_send_instagram_message", self._send_one)
        p.start()
        self.addCleanup(p.stop)

    def _send_one(self, sender_id, text):
        self.sent.append((sender_id, text))
        return self.results.pop(0) if self.results else (True, "")

    def send(self, text):
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = glam._send_instagram_reply(SENDER, text)
        return result, buf.getvalue()

    def test_short_reply_is_one_message_and_nothing_logged(self):
        text = "Singles: Clean Girl ₹249 · Kawaii ₹299 🤍"
        result, out = self.send(text)
        self.assertEqual(result, (True, ""))
        self.assertEqual(self.sent, [(SENDER, text)])
        self.assertNotIn("split", out)

    def test_1500_char_reply_is_two_messages_in_order_and_logged(self):
        text = reply_of(1500)
        result, out = self.send(text)
        self.assertEqual(result, (True, ""))
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(words(self.sent[0][1]) + words(self.sent[1][1]), words(text))
        self.assertIn("split into 2 messages", out)

    def test_3000_char_reply_is_two_messages_and_logged_as_cut(self):
        result, out = self.send(reply_of(3000))
        self.assertEqual(result, (True, ""))
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(self.sent[1][1].endswith(ENDING))
        self.assertIn("more details", out)

    def test_failed_first_part_stops_and_reports_failure(self):
        self.results = [(False, "HTTP 400: boom")]
        result, _ = self.send(reply_of(1500))
        self.assertEqual(result, (False, "HTTP 400: boom"))
        self.assertEqual(len(self.sent), 1)


if __name__ == "__main__":
    unittest.main()
