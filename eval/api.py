"""FastAPI wrapper around the eval harness.

    venv\\Scripts\\python.exe -m uvicorn eval.api:api --port 8100

Synchronous by design: 74 rows x 2 model calls finishes in minutes, so a
job queue would be complexity without a payoff. POST the run, get the
scored results back in the same response.

Importing this module imports eval.harness, which installs the outbound
guard before anything can run. No route here can send a customer message.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import harness

api = FastAPI(
    title="Glam Shelf Twin - Eval Harness",
    description=(
        "Re-runs the twin's real reply logic over the founder-labelled eval "
        "set and scores each fresh answer against the ideal answer with an "
        "LLM judge. Never contacts WhatsApp, Instagram or Telegram."
    ),
    version="1.0",
)


class RunRequest(BaseModel):
    judge: Literal["local_lora", "ollama", "groq"] = Field(
        "local_lora", description="Judge backend. ollama = local and free; groq = the official scored run."
    )
    category: Optional[str] = Field(
        None, description="Optional exact category filter, e.g. 'Refund'. Case-insensitive."
    )
    mode: Literal["fresh", "calibrate"] = Field(
        "fresh",
        description=(
            "fresh = generate new answers with draft_reply_logic and score them "
            "(the real eval). calibrate = score the recorded historical answers "
            "instead, to measure judge agreement against the founder's own verdicts."
        ),
    )
    limit: Optional[int] = Field(
        None, ge=1, description="Score only the first N matching rows. For smoke tests."
    )
    ideal_source: Optional[str] = Field(
        None,
        description=(
            "Filter by where the ground truth came from: 'founder-written' (the "
            "founder wrote a corrected reply) or 'as-sent' (the founder marked the "
            "sent reply Pass, so it became the target). Defaults to 'founder-written' "
            "in calibrate mode, because as-sent rows would have the judge compare a "
            "string to itself."
        ),
    )


@api.get("/health")
def health() -> dict:
    rows = harness.load_rows()
    return {
        "status": "ok",
        "eval_rows": len(rows),
        "categories": sorted({r["category"] for r in rows}),
        "judges": sorted(harness.JUDGES),
        "ollama_model": harness.OLLAMA_MODEL,
        "groq_model": harness.GROQ_MODEL,
        "sends_disabled": True,
    }


@api.post("/eval/run")
def eval_run(req: RunRequest) -> dict:
    try:
        return harness.run_eval(
            judge=req.judge,
            category=req.category,
            mode=req.mode,
            limit=req.limit,
            ideal_source=req.ideal_source,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        # e.g. GROQ_API_KEY missing - a configuration problem, not a bug.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
