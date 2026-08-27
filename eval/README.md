# Twin Eval Harness

Re-runs real customer questions through the twin's **current** reply logic, scores each fresh
answer against the founder's ideal answer with an LLM judge, and reports pass rate overall and
by category. 80 questions were labelled; 73 are scorable (see below).

> **This never touches the live WhatsApp or Instagram channels.** No route, node, or helper in
> `eval/` can send a customer message — see [Safety](#safety).

---

## Why it exists

The twin's answers drift as `brain.md`, pricing, inventory and the RAG index change. A passing
test suite proves the plumbing works; it does not prove the twin still gives the *right answer*
to "is the GS2 band thicker than GS1?". This harness measures that, and it measures it against
the founder's own written ground truth rather than a model's opinion of what sounds good.

## What it tests

- **Input**: 73 scorable rows from the finalized eval spreadsheet (`eval/data/eval_set.json`).
- **Under test**: `app.draft_reply_logic()` — the real entry point, with the real brain, live
  Shopify inventory, live policies and RAG retrieval. Not a mock.
- **Ground truth**: the founder's `Draft Ideal Answer`, with one repair described below.
- **Judge**: an LLM that returns `Pass` / `Partial` / `Fail` plus a one-line reason.

Every row is scored with **no conversation history**. This measures baseline per-question
correctness; thread-context behaviour is a separate kind of test.

### The two kinds of ground truth

The spreadsheet's `Draft Ideal Answer` column is not homogeneous, and this matters more than it
sounds:

| Rows | What the column holds | Handling |
|------|----------------------|----------|
| 35 | A real corrected reply, written because the sent answer was wrong | Used directly (`ideal_source: founder-written`) |
| 38 | A note *about* the reply — "As-sent is correct.", "As-sent is aligned." | The founder's Pass verdict certifies the sent reply, so the **sent reply** becomes the target and the note is kept as `ideal_note` (`ideal_source: as-sent (founder-certified Pass)`) |

Without that repair, half the eval set is ungradeable: no judge can score a freshly generated
customer message against the string "As-sent is correct." — it scores every one a mismatch, and
the pass rate collapses for reasons that have nothing to do with the twin. Nothing is invented
here; on those rows the founder-approved text *is* the ideal.

**Seven rows are excluded** rather than given a made-up target:

- IDs **26, 43, 51, 52, 78, 80** — no finalized ideal answer. Open policy gaps.
- ID **12** — the ideal column says "Same as row 11", a pointer with no Pass verdict to certify
  the sent reply, so there is nothing to ground it against.

## Results, and how to read them

First full run, 73 rows, no conversation history:

| Judge | Pass rate | Agreement with founder verdicts |
|-------|-----------|--------------------------------|
| `groq` / `openai/gpt-oss-120b` | **~31%** (23 Pass, 50 Fail, 0 Error) | 88.5% |
| `ollama` / `qwen3:8b` | 57.5% (42 Pass, 2 Partial, 29 Fail) | 26.9% |

Use the Groq number. The two disagree because the local 8B judge is far more lenient, and
calibration says it matches the founder's grading barely a quarter of the time. Two Groq runs
scored 30.6% and 31.5%, so read it as ~31% ±1 — `draft_reply_logic` is not deterministic.

Split by where the ground truth came from:

| Ground truth | n | Pass rate |
|--------------|---|-----------|
| `founder-written` (the founder rewrote the reply) | 35 | 0.200 |
| `as-sent` (the founder marked the reply Pass) | 38 | 0.421 |

The founder-written rows are cases the twin already got wrong once, so a low score there is
expected rather than alarming.

### Three things that number does not mean

**1. The 88.5% calibration figure is in-sample, not an accuracy estimate.** The completeness
rule that produced it was written *after* looking at the same 26 labelled rows it is measured
on, and tuned until the disagreements dropped. That is a fit, not a held-out result. A genuine
estimate of judge accuracy needs rows the rubric was never tuned against. Treat 88.5% as
"the rubric now encodes how the founder grades", not as "the judge is 88.5% correct."

**2. Eight of the 50 failures are a product question, not a confirmed defect.** On rows 1, 4,
5, 6, 28, 32, 40 and 55 the twin got every fact right and failed solely because it dropped a
follow-up question — "Which occasion are you shopping for?" and similar. The completeness rule
counts that as a failure because the ideal contained it. Whether a sales bot that states the
right facts but stops asking the next question is *wrong* is a decision about what the product
should do, and it belongs to the founder, not the judge. Re-scoring with those eight treated as
passes moves the headline from ~31% to ~42%.

**3. The verdict is trustworthy; the stated reason is not always.** Pass/Fail tracks the
founder's own grading closely, but the one-line `reason` can be wrong even when the verdict is
right. On ID 56 the judge failed the answer for "inventing" a ₹749/tray bulk rate — that rate is
real and appears verbatim in row 23's own ideal answer. The Fail was still correct, for a
different reason (the answer omitted the price range and the ₹799 threshold). The judge treats
"not present in *this row's* ideal" as "invented", so it can mark down true information.
**Anyone triaging failures from this output should read the row before acting on the reason.**

## Setup

```
venv\Scripts\python.exe -m pip install -r eval/requirements.txt
```

Regenerate the eval set from the spreadsheet (one-time; already committed):

```
venv\Scripts\python.exe eval/convert.py "C:\path\to\twin_eval_labels_v2.xlsx"
```

## Running

```
venv\Scripts\python.exe -m uvicorn eval.api:api --port 8100
```

| Route | What it does |
|-------|--------------|
| `GET /health` | Row count, categories, configured judges. No model calls. |
| `POST /eval/run` | Runs the graph and returns the full scored results. |

`POST /eval/run` body:

| Field | Values | Meaning |
|-------|--------|---------|
| `judge` | `ollama` \| `groq` | `ollama` is local and free — use it while iterating. `groq` is the official scored run. |
| `category` | e.g. `"Refund"` | Optional. Score one category only. |
| `mode` | `fresh` \| `calibrate` | `fresh` generates new answers and scores them. `calibrate` scores the *recorded historical* answers instead, to check the judge against the founder's own verdicts. |
| `limit` | integer | Optional. First N matching rows, for smoke tests. |
| `ideal_source` | `founder-written` \| `as-sent` | Optional. Score only rows whose ground truth came from that source. Defaults to `founder-written` in `calibrate` mode. |

```bash
curl -X POST http://localhost:8100/eval/run -H "Content-Type: application/json" -d "{\"judge\":\"ollama\"}"
```

Synchronous by design — 73 rows finishes in about ten minutes, so a job queue would be
complexity without a payoff.

### Judge configuration

| Judge | Default model | Override |
|-------|---------------|----------|
| `ollama` | `qwen3:8b` at `http://localhost:11434` | `OLLAMA_MODEL`, `OLLAMA_URL` |
| `groq` | `openai/gpt-oss-120b` | `GROQ_MODEL`, needs `GROQ_API_KEY` in `.env` |

### Calibrating the judge

An eval is only as good as its grader. `mode: "calibrate"` re-scores the answers the twin
*actually gave* and compares the judge's verdict to the founder's own, returning a
`judge_agreement` block — agreement rate plus every disagreement.

It runs only on `founder-written` rows. The as-sent rows are degenerate in this mode: their
ideal *is* the recorded answer, so the judge would be comparing a string to itself and scoring
a free Pass.

What calibration actually caught, in order:

| Judge + rubric | Agreement with founder (26 labelled rows) |
|----------------|--------------------------------------------|
| `qwen3:8b`, first rubric | 27% |
| `gpt-oss-120b`, first rubric | 54% |
| `gpt-oss-120b`, + completeness rule | **88%** |

The jump from 54% to 88% was not a better model — it was fixing the rubric. The judge kept
scoring `Partial` where the founder scored `Fail`, always with the same reason: *"omits X
present in the ideal."* The founder treats a reply that leaves out a required element — a price,
a threshold, a follow-up question, a hand-off to a human — as a failure, not a partial success.
Once the rubric said so, the disagreements collapsed.

Two honest caveats:

- **That rubric was tuned on these same 26 rows**, so 88% is in-sample. It is a fit, not a
  held-out estimate of judge accuracy.
- **Some founder verdicts are not recoverable from the data.** On ID 60 the sent reply is the
  ideal answer verbatim plus "Thank you for reaching out." at the front, and the founder failed
  it. Whatever rule that encodes lives outside the spreadsheet, so no judge can learn it from
  these columns. If eval ground truth is going to carry brand rules, the rule needs a column of
  its own.

## Output

```jsonc
{
  "judge": "groq", "judge_model": "openai/gpt-oss-120b", "rows_scored": 73,
  "summary": {
    "overall":     { "Pass": 0, "Partial": 0, "Fail": 0, "pass_rate": 0.0 },
    "by_category": { "Refund": { "...": 0 } },
    "by_channel":  { "WhatsApp": { "...": 0 } },
    "judge_agreement": { "agreement_rate": 0.0, "disagreements": [] }
  },
  "results": [ { "id": "1", "verdict": "Pass", "reason": "...", "fresh_answer": "...", "...": "" } ]
}
```

`pass_rate` counts strict `Pass` over rows the judge could score; `Partial` does not count as a
pass, and `Error` rows are excluded from the denominator rather than counted as failures.

## Safety

The harness must never message a real customer. Two independent guarantees:

1. **`draft_reply_logic` has no send path.** It loads the brain, inventory, policies and RAG
   context, calls the model, and returns a string. Every WhatsApp / Instagram / Telegram send
   lives in the *webhook handlers* that call it — and this harness never invokes a handler.
2. **`_install_outbound_guard()`** replaces `send_whatsapp_reply`, `send_whatsapp_template`,
   `_send_instagram_reply`, `send_telegram_notification`, `send_draft_for_approval`,
   `_reassign_to_bot` and `_send_review_request` with functions that **raise**. If a future
   refactor moves a send into the generate path, the run aborts loudly instead of messaging
   someone.

`harness.py` also blanks `GITHUB_TOKEN` / `GITHUB_REPO` and points `DB_PATH` at a temp file
*before* importing `app`, because importing `app` runs `_restore_db_from_github()` and starts
the hourly backup loop. Without that guard an eval run could pull down and re-push the
production SQLite.

## Files

```
eval/
├── convert.py          # one-time xlsx -> eval_set.json
├── harness.py          # LangGraph: generate -> judge, plus scoring
├── api.py              # FastAPI: GET /health, POST /eval/run
├── requirements.txt    # eval-only deps; NOT installed on Render
└── data/eval_set.json  # 73 scorable rows
```

Local only. Not deployed, and nothing in `app.py` imports from this directory.
