"""LangGraph eval harness for the Glam Shelf twin.

Two nodes per row:

    generate  -> call app.draft_reply_logic() for a fresh reply
    judge     -> compare that reply to the founder's ideal answer,
                 return Pass / Partial / Fail + a one-line reason

NOTHING HERE CAN SEND A CUSTOMER MESSAGE. Two independent reasons:

  1. draft_reply_logic (app.py:4148-4244) contains no send path. It loads
     the brain, live inventory, live policies and RAG context, calls the
     model, and returns (classification, reply, raw_response). Every
     WhatsApp / Instagram / Telegram send lives in the webhook handlers
     that call it, and this harness never invokes a webhook handler.
  2. _install_outbound_guard() below replaces every outbound function in
     app with one that raises. If a future refactor ever moves a send
     into the generate path, this run fails loudly instead of messaging
     a real customer. Installed lazily by run_eval() — NEVER at import
     time (see run_eval for why: a Sep 2026 incident where importing
     this module alone, from app.py's live reply path, permanently
     broke production sends for the life of the process).

Importing app also runs module-level side effects (app.py:2396-2398:
_restore_db_from_github, _init_db, _start_backup_loop), so the env guard
below blanks the GitHub credentials and points DB_PATH at a scratch file
BEFORE the import. Without that, an eval run could pull down and re-push
the production SQLite. Same pattern as tests/test_pause_gate.py.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional, TypedDict

# ---------------------------------------------------------------------------
# Env guard - must run before `import app`.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="glamshelf-eval-"), "eval.db"
)
os.environ["GITHUB_TOKEN"] = ""   # disables _restore_db_from_github + backup push
os.environ["GITHUB_REPO"] = ""
os.environ.setdefault("SECRET_KEY", "eval")
os.environ.setdefault("APP_PASSWORD", "eval")
os.environ.setdefault("DASHBOARD_KEY", "eval")

import requests  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402

import app  # noqa: E402

DATA_FILE = Path(__file__).resolve().parent / "data" / "eval_set.json"

# Every function in app that can reach a real customer or the founder.
_OUTBOUND = [
    "send_whatsapp_reply",
    "send_whatsapp_template",
    "_send_instagram_reply",
    "send_telegram_notification",
    "send_draft_for_approval",
    "_reassign_to_bot",
    "_send_review_request",
]


def _install_outbound_guard() -> None:
    """Replace outbound functions with raisers, and audit writes with no-ops."""

    def blocked(name: str) -> Callable[..., Any]:
        def guard(*_a: Any, **_k: Any):
            raise RuntimeError(
                f"EVAL SAFETY: app.{name}() was called during an eval run. "
                "The harness must never contact a real channel. Aborting."
            )
        return guard

    for name in _OUTBOUND:
        if hasattr(app, name):
            setattr(app, name, blocked(name))

    for name in ("_log_message", "_log_instagram"):
        if hasattr(app, name):
            setattr(app, name, lambda *_a, **_k: None)


# ---------------------------------------------------------------------------
# Judge backends
# ---------------------------------------------------------------------------
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:8b")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")

JUDGE_SYSTEM = (
    "You are a strict but fair quality judge for a customer support agent's "
    "replies. Given a customer question, the ideal answer, and the agent's "
    "candidate answer, judge on two separate dimensions: (1) factual "
    "correctness — does the candidate answer contradict the ideal answer's "
    "facts (wrong price, wrong product, wrong policy, wrong process)? (2) "
    "completeness — does the ideal answer contain a required element (a "
    "specific link, a specific caveat, a specific routing) that the "
    "candidate answer omits? A reply that's factually accurate but missing a "
    "required element is not a full Pass.\n\n"
    "Reply with JSON only, of the form "
    "{\"verdict\": \"Pass|Fail\", \"reason\": \"one short sentence\"}"
)


def _judge_prompt(question: str, candidate: str, ideal: str) -> str:
    return (
        f"CUSTOMER QUESTION:\n{question}\n\n"
        f"IDEAL ANSWER (ground truth):\n{ideal}\n\n"
        f"CANDIDATE ANSWER (system under test):\n{candidate}\n\n"
        "Grade the candidate. JSON only."
    )


def _parse_verdict(raw: str) -> tuple[str, str]:
    """Pull {verdict, reason} out of a model response, tolerating fences and
    <think> blocks (qwen3 can emit these even with thinking disabled)."""
    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.S).strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return "Error", f"unparseable judge output: {raw[:120]!r}"
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return "Error", f"invalid JSON from judge: {match.group(0)[:120]!r}"

    verdict = str(data.get("verdict", "")).strip().capitalize()
    if verdict not in ("Pass", "Partial", "Fail"):
        return "Error", f"unknown verdict {data.get('verdict')!r}"
    return verdict, str(data.get("reason", "")).strip()


def _judge_ollama(question: str, candidate: str, ideal: str) -> tuple[str, str]:
    resp = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": OLLAMA_MODEL,
            "messages": [
                {"role": "system", "content": JUDGE_SYSTEM},
                {"role": "user", "content": _judge_prompt(question, candidate, ideal)},
            ],
            "stream": False,
            "think": False,
            "format": "json",
            "options": {"temperature": 0},
        },
        timeout=180,
    )
    resp.raise_for_status()
    return _parse_verdict(resp.json()["message"]["content"])


def _judge_groq(question: str, candidate: str, ideal: str) -> tuple[str, str]:
    from groq import Groq
    from groq import RateLimitError

    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        raise RuntimeError(
            "GROQ_API_KEY is not set. Add it to .env, or run with judge='ollama'."
        )
    client = Groq(api_key=key)

    # The free tier caps tokens-per-minute, and a 73-row run reliably clips it.
    # A rate-limited row is a hole in the scored set, not a real verdict, so
    # back off and retry rather than recording an Error.
    delay = 2.0
    for attempt in range(5):
        try:
            completion = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {"role": "system", "content": JUDGE_SYSTEM},
                    {"role": "user", "content": _judge_prompt(question, candidate, ideal)},
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
            return _parse_verdict(completion.choices[0].message.content or "")
        except RateLimitError:
            if attempt == 4:
                raise
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


JUDGES: dict[str, Callable[[str, str, str], tuple[str, str]]] = {
    "ollama": _judge_ollama,
    "groq": _judge_groq,
}


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------
class RowState(TypedDict, total=False):
    id: str
    channel: str
    category: str
    question: str
    ideal_answer: str
    actual_answer: str
    human_verdict: Optional[str]

    judge: str
    mode: str            # "fresh" (default) or "calibrate"

    fresh_answer: str
    classification: str
    verdict: str
    reason: str
    error: str
    seconds: float


def generate_node(state: RowState) -> dict:
    """Produce the answer to be graded.

    mode="fresh"     -> call the twin's real reply logic (the thing under test)
    mode="calibrate" -> reuse the recorded historical answer, so the judge can
                        be scored against the founder's own verdicts
    """
    if state.get("mode") == "calibrate":
        return {
            "fresh_answer": state.get("actual_answer", ""),
            "classification": "(recorded)",
        }

    started = time.time()
    try:
        classification, reply, _raw = app.draft_reply_logic(
            message=state["question"],
            order_context="",
            history=None,                     # baseline correctness, no thread context
            source=state.get("channel") or "WhatsApp",
        )
    except Exception as exc:  # noqa: BLE001 - one bad row must not kill the run
        return {
            "error": f"generate failed: {type(exc).__name__}: {exc}",
            "seconds": round(time.time() - started, 2),
        }
    return {
        "fresh_answer": reply,
        "classification": classification,
        "seconds": round(time.time() - started, 2),
    }


def judge_node(state: RowState) -> dict:
    if state.get("error"):
        return {"verdict": "Error", "reason": state["error"]}

    candidate = (state.get("fresh_answer") or "").strip()
    if not candidate:
        return {"verdict": "Fail", "reason": "twin produced an empty reply"}

    try:
        verdict, reason = JUDGES[state["judge"]](
            state["question"], candidate, state["ideal_answer"]
        )
    except Exception as exc:  # noqa: BLE001
        return {"verdict": "Error", "reason": f"judge failed: {type(exc).__name__}: {exc}"}
    return {"verdict": verdict, "reason": reason}


def build_graph():
    g = StateGraph(RowState)
    g.add_node("generate", generate_node)
    g.add_node("judge", judge_node)
    g.add_edge(START, "generate")
    g.add_edge("generate", "judge")
    g.add_edge("judge", END)
    return g.compile()


GRAPH = build_graph()


# ---------------------------------------------------------------------------
# Run + aggregate
# ---------------------------------------------------------------------------
def load_rows(category: str | None = None,
              ideal_source: str | None = None) -> list[dict]:
    rows = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    if category:
        wanted = category.strip().lower()
        rows = [r for r in rows if r["category"].lower() == wanted]
    if ideal_source:
        rows = [r for r in rows if r.get("ideal_source", "").startswith(ideal_source)]
    return rows


def run_eval(judge: str = "groq", category: str | None = None,
             mode: str = "fresh", limit: int | None = None,
             ideal_source: str | None = None) -> dict:
    # Installed here, not at module import time: importing this module
    # (e.g. transitively, from anything that isn't actually starting an
    # eval run) must never touch app's send functions. See the Sep 2026
    # incident note in the module docstring above.
    _install_outbound_guard()

    if judge not in JUDGES:
        raise ValueError(f"unknown judge {judge!r}; expected one of {sorted(JUDGES)}")

    # In calibrate mode the as-sent rows are degenerate: their ideal IS the
    # recorded answer, so the judge would be comparing a string to itself.
    # Only founder-written ideals carry calibration signal.
    if mode == "calibrate" and ideal_source is None:
        ideal_source = "founder-written"

    rows = load_rows(category, ideal_source)
    if limit:
        rows = rows[:limit]
    if not rows:
        return {
            "judge": judge, "mode": mode, "category": category,
            "ideal_source": ideal_source,
            "rows_scored": 0, "results": [], "summary": {},
            "note": "no rows matched those filters",
        }

    started = time.time()
    results = []
    for row in rows:
        state: RowState = {**row, "judge": judge, "mode": mode}
        out = GRAPH.invoke(state)
        results.append({
            "id": out["id"],
            "channel": out["channel"],
            "category": out["category"],
            "question": out["question"],
            "ideal_answer": out["ideal_answer"],
            "fresh_answer": out.get("fresh_answer", ""),
            "classification": out.get("classification", ""),
            "verdict": out.get("verdict", "Error"),
            "reason": out.get("reason", ""),
            "human_verdict": out.get("human_verdict"),
            "seconds": out.get("seconds", 0.0),
        })

    return {
        "judge": judge,
        "judge_model": OLLAMA_MODEL if judge == "ollama" else GROQ_MODEL,
        "mode": mode,
        "category": category,
        "ideal_source": ideal_source,
        "rows_scored": len(results),
        "elapsed_seconds": round(time.time() - started, 1),
        "summary": summarize(results, mode=mode),
        "results": results,
    }


def _tally(items: list[dict]) -> dict:
    counts = {"Pass": 0, "Partial": 0, "Fail": 0, "Error": 0}
    for r in items:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    scored = len(items) - counts["Error"]
    return {
        **counts,
        "total": len(items),
        # Pass rate counts strict passes over the rows the judge could score.
        "pass_rate": round(counts["Pass"] / scored, 3) if scored else None,
    }


def summarize(results: list[dict], mode: str = "fresh") -> dict:
    by_category: dict[str, list[dict]] = {}
    by_channel: dict[str, list[dict]] = {}
    for r in results:
        by_category.setdefault(r["category"], []).append(r)
        by_channel.setdefault(r["channel"], []).append(r)

    summary = {
        "overall": _tally(results),
        "by_category": {k: _tally(v) for k, v in sorted(by_category.items())},
        "by_channel": {k: _tally(v) for k, v in sorted(by_channel.items())},
    }

    # Judge-vs-founder agreement, on rows carrying a human label.
    #
    # Only meaningful in calibrate mode. In fresh mode the judge is grading a
    # newly generated answer while the human verdict refers to the answer that
    # was sent months ago - two different texts, so an "agreement rate" between
    # them would compare nothing to nothing.
    labelled = [r for r in results if r.get("human_verdict") and r["verdict"] != "Error"]
    if labelled and mode == "calibrate":
        agree = sum(1 for r in labelled if r["verdict"] == r["human_verdict"])
        summary["judge_agreement"] = {
            "rows_with_human_label": len(labelled),
            "agreements": agree,
            "agreement_rate": round(agree / len(labelled), 3),
            "disagreements": [
                {"id": r["id"], "human": r["human_verdict"],
                 "judge": r["verdict"], "reason": r["reason"]}
                for r in labelled if r["verdict"] != r["human_verdict"]
            ],
        }
    return summary
