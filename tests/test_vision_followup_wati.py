"""Reconstruction of the two photo-then-text incidents on the WATI path.

A customer sends a photo, then types what they want a few seconds later.
WATI delivers those as two INDEPENDENT webhook events, so a single
draft_reply_logic() call can't reproduce either failure — both need the
real /webhook route driven twice in sequence, against a sandbox DB, with
the vision API mocked (the original images are long gone from WATI's CDN).

Incidents reconstructed:

  A) 2026-07-31, wa_id …1290 — product photo, nothing extractable.
     Before: vision returned high confidence with zero fields, the generic
     branch framed it to the twin as "I just sent a screenshot of my order
     — no specific details visible", and 3 seconds later "I want this
     lashes" was answered by asking her to share a photo she had already
     sent.
     After: the photo is acknowledged on its own terms, and the follow-up
     text carries the vision context forward.

  B) 2026-06-09, wa_id …8087 — product photo, product identifiable,
     followed by "I want to buy this lashes". The twin must not ask which
     one when vision already named it.

Network reality: the vision call and WATI sends are stubbed; the REPLY
model (DeepSeek) is called for real, because the thing under test is what
the twin actually says. Each scenario therefore runs REPEAT times and every
run must pass. Shopify-backed context (inventory, policies) and RAG are
stubbed to "" so the run doesn't depend on live store credentials.

Infrastructure retry — read this before touching it:

    Calling a live model means some runs fail for reasons that have nothing
    to do with what the twin said: the API times out, returns nothing, or
    returns a 200 whose body isn't valid JSON. draft_reply_logic turns that
    last case into ("", ""), the webhook dispatches nothing, and the run
    looks identical to "the twin stayed silent" — which is how this test
    was intermittently failing.

    So the sequence is retried, but ONLY on that infrastructure class:
    the call raised, the response was empty, or the response didn't parse.
    A response that parsed fine and simply said something unexpected is
    NOT retried — content assertions fail on the first attempt, at full
    strength. Retrying those would be tuning the test until the model
    agreed with it.

    A persistent outage still fails the test: after VISION_FOLLOWUP_API_RETRIES
    attempts the empty result is returned as-is and the existing assertions
    fire, with the reason reported.

    (Production has a narrower version of this same gap — the OpenAI SDK
    retries transport failures twice by default, but an unparseable 200 is
    not retried and the customer is sent nothing. That is app.py's problem,
    not this file's; see the notes on ask_claude.)

Run:  python -m unittest tests.test_vision_followup_wati
      (needs DEEPSEEK_API_KEY; set VISION_FOLLOWUP_REPEAT to change runs,
       VISION_FOLLOWUP_API_RETRIES to change the infrastructure retry budget)
"""

import os
import re
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="glamshelf-vision-followup-test-"), "test.db"
)
os.environ["GITHUB_TOKEN"] = ""
os.environ["GITHUB_REPO"] = ""
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("APP_PASSWORD", "test-app-password")
os.environ.setdefault("DASHBOARD_KEY", "test-dashboard-key")

import app as glam  # noqa: E402

REPEAT = int(os.environ.get("VISION_FOLLOWUP_REPEAT", "5"))

# Attempts per sequence when the live API fails in an infrastructure way.
# Not a budget for disagreeing with a content assertion — see module docstring.
API_RETRIES = int(os.environ.get("VISION_FOLLOWUP_API_RETRIES", "3"))
API_RETRY_BACKOFF = float(os.environ.get("VISION_FOLLOWUP_API_BACKOFF", "2.0"))

WA_ID_A = "918766231290"   # July 31 incident
WA_ID_B = "918655148087"   # June 9 incident

FAKE_IMAGE_URL = "https://example.invalid/wati-media/fake.jpg"

# What the twin must never do right after a customer sent a photo:
# ask for a photo. Matches "could you share a photo", "please send us a
# picture", "upload an image" — but NOT "thanks for the photo you sent".
ASKS_FOR_PHOTO = re.compile(
    r"(send|share|upload|attach|drop)\b[^.?!]{0,60}\b(photo|picture|image|pic|screenshot)",
    re.IGNORECASE,
)

# The June 9 failure mode: asking which product when vision already named it.
ASKS_WHICH_PRODUCT = re.compile(
    r"which\b[^.?!]{0,30}\b(one|style|lash|lashes|product|model|design)",
    re.IGNORECASE,
)


def image_event(wa_id, *, caption=None, msg_id=""):
    payload = {
        "type": "image",
        "waId": wa_id,
        "senderName": "Reconstruction Test",
        "id": msg_id,
        "data": FAKE_IMAGE_URL,
    }
    if caption is not None:
        payload["text"] = caption
    return payload


def text_event(wa_id, body, *, msg_id=""):
    return {
        "type": "text",
        "waId": wa_id,
        "senderName": "Reconstruction Test",
        "id": msg_id,
        "text": body,
    }


class VisionFollowUpReconstruction(unittest.TestCase):
    """Drives the real /webhook route twice per scenario."""

    def setUp(self):
        os.environ["WATI_WEBHOOK_VERIFY_DISABLED"] = "1"
        self.addCleanup(os.environ.pop, "WATI_WEBHOOK_VERIFY_DISABLED", None)
        if not os.environ.get("DEEPSEEK_API_KEY"):
            self.skipTest("DEEPSEEK_API_KEY not set — reply model unavailable")
        self._reset_state()

    def _reset_state(self):
        """Fresh conversation for every run: no carry-over history, no
        carry-over vision context, no dedup collisions."""
        glam._recent_vision_context.clear()
        conn = sqlite3.connect(glam.DB_PATH)
        conn.execute("DELETE FROM message_logs")
        conn.commit()
        conn.close()

    def _run_sequence(self, wa_id, vision_result, follow_up_text, *, caption=None):
        """Post an image event then a text event, as WATI would. Returns
        (reply_to_image, reply_to_text, infra_failure) — the text actually
        dispatched to the customer on each event (or "" if nothing was
        sent), plus a description of any infrastructure-class LLM failure
        seen during the sequence, or None if the model answered cleanly.

        `infra_failure` is what makes the retry in _run_sequence_retrying
        safe: it is set ONLY when the model call raised, came back empty,
        or came back unparseable. A response that parsed and simply said
        something the assertions dislike leaves it None, so that outcome
        is never retried away."""
        sends = []
        infra = []
        classifications = []

        def fake_send(number, text):
            sends.append((number, text))
            return (True, "")

        real_draft = glam.draft_reply_logic

        def watched_draft(message, order_context="", history=None, source="WhatsApp"):
            """Passthrough spy. Runs the REAL pipeline — including the real
            DeepSeek call — and only classifies how it failed, if it did."""
            try:
                classification, reply, raw = real_draft(
                    message, order_context=order_context,
                    history=history, source=source,
                )
            except Exception as exc:                      # timeout, connection, API error
                infra.append(f"{type(exc).__name__}: {exc}")
                raise
            if not (raw or "").strip():
                infra.append("model returned an empty response")
            elif not classification and not reply:
                # draft_reply_logic swallows JSONDecodeError and hands back
                # ("", ""), so an unparseable 200 looks exactly like silence.
                infra.append(f"model response did not parse as JSON: {raw[:160]!r}")
            # Recorded for every call, not just failures: a non-AUTO verdict
            # also produces no send_whatsapp_reply (DRAFT+APPROVE goes to
            # Telegram), which is indistinguishable from silence at the
            # dispatch layer and must NOT be mistaken for an API problem.
            classifications.append(classification or "<unparsed>")
            return classification, reply, raw

        patches = [
            # --- vision: mocked, no real image, no real API call ---
            patch.object(glam, "_extract_wati_image_url", lambda data: FAKE_IMAGE_URL),
            patch.object(glam, "_extract_image_info", lambda url: dict(vision_result)),
            # --- outbound: recorded, never sent ---
            patch.object(glam, "send_whatsapp_reply", fake_send),
            patch.object(glam, "send_telegram_notification", lambda *a, **k: None),
            patch.object(glam, "send_draft_for_approval", lambda *a, **k: True),
            # --- takeover / routing gates: inert ---
            patch.object(glam, "_is_outbound_event", lambda data: False),
            patch.object(glam, "_handle_pause_directive", lambda *a, **k: None),
            patch.object(glam, "_is_paused", lambda wa: False),
            patch.object(glam, "_udit_replied_recently", lambda wa: False),
            patch.object(glam, "_check_recent_human_reply", lambda wa: False),
            patch.object(glam, "_pause_number", lambda *a, **k: None),
            patch.object(glam, "_reassign_to_bot", lambda *a, **k: None),
            # --- live-store context: stubbed, keeps the run offline-ish ---
            patch.object(glam, "_lookup_recent_order", lambda wa: ""),
            patch.object(glam, "get_live_inventory", lambda: ""),
            patch.object(glam, "get_live_policies", lambda: ""),
            patch.object(glam, "_rag_retrieve", lambda msg: ""),
            # Observes the real call; does not replace it.
            patch.object(glam, "draft_reply_logic", watched_draft),
        ]
        # _log_message and _load_wati_history run for real against the
        # sandbox DB — the cross-event history behaviour is part of what
        # this reconstruction is checking.
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            client = glam.app.test_client()
            r1 = client.post("/webhook", json=image_event(wa_id, caption=caption))
            self.assertEqual(r1.status_code, 200)
            first = sends[-1][1] if sends else ""

            r2 = client.post("/webhook", json=text_event(wa_id, follow_up_text))
            self.assertEqual(r2.status_code, 200)
            second = sends[-1][1] if len(sends) > 1 else ""
        self._last_classifications = list(classifications)
        return first, second, ("; ".join(infra) if infra else None)

    def _run_sequence_retrying(self, *args, **kwargs):
        """_run_sequence, retried on infrastructure failures only.

        Content outcomes are returned from the first attempt that reaches
        the model successfully — they are never re-rolled. If every attempt
        hits infrastructure trouble, the last (empty) result is returned so
        the caller's assertions still fail, and the reason is printed."""
        last = None
        for attempt in range(1, API_RETRIES + 1):
            first, second, infra = self._run_sequence(*args, **kwargs)
            last = (first, second)
            if infra is None:
                return last
            print(f"[INFRA] attempt {attempt}/{API_RETRIES} — {infra}")
            if attempt < API_RETRIES:
                time.sleep(API_RETRY_BACKOFF * attempt)
                self._reset_state()
        print(f"[INFRA] giving up after {API_RETRIES} attempts — reporting as a failure")
        return last

    # ------------------------------------------------------------------
    # Incident A — 2026-07-31, unidentifiable product photo
    # ------------------------------------------------------------------
    def test_incident_a_photo_then_i_want_this_lashes(self):
        vision = {
            "image_type": "product_photo",
            "order_id": None, "payment_status": None, "amount": None,
            "product": None, "customer_name": None, "eye_shape": None,
            "confidence": "high",
        }
        failures = []
        for run in range(1, REPEAT + 1):
            self._reset_state()
            first, second = self._run_sequence_retrying(
                WA_ID_A, vision, "I want this lashes"
            )
            print(f"\n[RUN A{run}] image reply: {first!r}")
            print(f"[RUN A{run}] text  reply: {second!r}")
            print(f"[RUN A{run}] classifications: {self._last_classifications}")

            if "screenshot of my order" in first.lower():
                failures.append(f"A{run}: image reply used order-screenshot framing")
            if first != glam.PRODUCT_PHOTO_REPLY:
                failures.append(f"A{run}: image reply was not PRODUCT_PHOTO_REPLY: {first!r}")
            if not second:
                failures.append(
                    f"A{run}: follow-up text got no reply at all "
                    f"(classifications={self._last_classifications})"
                )
            if ASKS_FOR_PHOTO.search(second):
                failures.append(f"A{run}: follow-up reply asked for a photo: {second!r}")
        self.assertEqual(failures, [], "\n".join(failures))

    # ------------------------------------------------------------------
    # Incident B — 2026-06-09, identifiable product photo
    # ------------------------------------------------------------------
    def test_incident_b_photo_then_i_want_to_buy_this_lashes(self):
        vision = {
            "image_type": "product_photo",
            "order_id": None, "payment_status": None, "amount": None,
            "product": "GS1 Luxe Light Lash Tray", "customer_name": None,
            "eye_shape": None, "confidence": "high",
        }
        failures = []
        for run in range(1, REPEAT + 1):
            self._reset_state()
            first, second = self._run_sequence_retrying(
                WA_ID_B, vision, "I want to buy this lashes"
            )
            print(f"\n[RUN B{run}] image reply: {first!r}")
            print(f"[RUN B{run}] text  reply: {second!r}")
            print(f"[RUN B{run}] classifications: {self._last_classifications}")

            if not first:
                failures.append(f"B{run}: image event produced no reply")
            if not second:
                failures.append(
                    f"B{run}: follow-up text got no reply at all "
                    f"(classifications={self._last_classifications})"
                )
            if ASKS_FOR_PHOTO.search(second):
                failures.append(f"B{run}: follow-up reply asked for a photo: {second!r}")
            if ASKS_WHICH_PRODUCT.search(second):
                failures.append(
                    f"B{run}: follow-up reply asked which product although vision "
                    f"named it: {second!r}"
                )
        self.assertEqual(failures, [], "\n".join(failures))


class VisionBranchUnitTests(unittest.TestCase):
    """Deterministic checks that need no LLM call."""

    def test_caption_is_appended_not_overwritten(self):
        """A caption typed onto the image survives the vision branch."""
        captured = {}

        def fake_draft(message, order_context="", history=None, source="WhatsApp"):
            captured["message"] = message
            return ("AUTO", "ok", "{}")

        os.environ["WATI_WEBHOOK_VERIFY_DISABLED"] = "1"
        self.addCleanup(os.environ.pop, "WATI_WEBHOOK_VERIFY_DISABLED", None)
        glam._recent_vision_context.clear()
        vision = {
            "image_type": "product_photo", "order_id": None, "payment_status": None,
            "amount": None, "product": "GS2 Volume Tray", "customer_name": None,
            "eye_shape": None, "confidence": "high",
        }
        patches = [
            patch.object(glam, "_extract_wati_image_url", lambda data: FAKE_IMAGE_URL),
            patch.object(glam, "_extract_image_info", lambda url: dict(vision)),
            patch.object(glam, "send_whatsapp_reply", lambda n, t: (True, "")),
            patch.object(glam, "_is_outbound_event", lambda data: False),
            patch.object(glam, "_handle_pause_directive", lambda *a, **k: None),
            patch.object(glam, "_is_paused", lambda wa: False),
            patch.object(glam, "_udit_replied_recently", lambda wa: False),
            patch.object(glam, "_check_recent_human_reply", lambda wa: False),
            patch.object(glam, "_load_wati_history", lambda wa: []),
            patch.object(glam, "_lookup_recent_order", lambda wa: ""),
            patch.object(glam, "_log_message", lambda *a, **k: None),
            patch.object(glam, "draft_reply_logic", fake_draft),
        ]
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            client = glam.app.test_client()
            resp = client.post(
                "/webhook",
                json=image_event(WA_ID_A, caption="do you have this in a pair?"),
            )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("do you have this in a pair?", captured.get("message", ""))
        self.assertIn("GS2 Volume Tray", captured["message"])

    def test_nested_caption_shapes_are_read(self):
        self.assertEqual(
            glam._extract_wati_caption({"caption": " hi there "}), "hi there"
        )
        self.assertEqual(
            glam._extract_wati_caption({"data": {"url": "x", "caption": "nested"}}),
            "nested",
        )
        self.assertEqual(glam._extract_wati_caption({"data": "https://x/y.jpg"}), "")

    def test_vision_context_expires(self):
        glam._recent_vision_context.clear()
        glam._remember_vision_context(WA_ID_A, "sent a photo of lashes")
        self.assertIn("lashes", glam._recall_vision_context(WA_ID_A))
        # Age the entry past the TTL.
        ts, summary = glam._recent_vision_context[WA_ID_A]
        glam._recent_vision_context[WA_ID_A] = (
            ts - glam.VISION_CONTEXT_TTL_SECONDS - 1, summary
        )
        self.assertEqual(glam._recall_vision_context(WA_ID_A), "")
        self.assertNotIn(WA_ID_A, glam._recent_vision_context)


if __name__ == "__main__":
    unittest.main()
