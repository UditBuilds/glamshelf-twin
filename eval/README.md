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
- **Judge**: an LLM that returns `Pass` / `Fail` plus a one-line reason. Until Sept 1 its
  prompt also allowed `Partial`, and the parser still accepts it.

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

Two measurements so far, on different dates and with different judge prompts. Read the dates
before comparing them.

| Date | What was measured | Judge prompt | Run | Pass | Fail | Error | Pass rate |
|------|-------------------|--------------|-----|-----:|-----:|------:|----------:|
| Aug 27, 2026 | main on Aug 27 | Aug 27 prompt (Pass / Partial / Fail) | first run | 23 | 50 | 0 | **~31%** |
| Oct 13, 2026 | main at `6020143` (after PR 63) | today's prompt (Pass / Fail, Sept 1) | run A | 25 | 48 | 0 | 34.2% |
| Oct 13, 2026 | main at `6020143` (after PR 63) | today's prompt (Pass / Fail, Sept 1) | run B | 25 | 48 | 0 | 34.2% |
| Oct 13, 2026 | | | **mean of A and B** | 25 | 48 | 0 | **34.2%** |

All 73 rows, no conversation history, judged by `openai/gpt-oss-120b` on Groq at temperature 0
both times.

### Oct 13, 2026: re-run on today's main

**There is no same-judge before/after.** Between the two dates only the judge's system prompt
changed (commit `31aea62`, Sept 1: the three-way rubric became a two-dimension Pass/Fail one).
The judge model, the user prompt, the verdict parsing and the Groq call are the same. The
Aug 27 per-row answers were never saved (the harness then returned results over HTTP only;
`eval/runs/` arrived on Oct 8), so they cannot be re-scored with today's prompt. The move from
~31% to 34% mixes changes to the twin (the Sept 28 audit and the audit-batch PRs 49–58) with a
change to the judge, and this data cannot separate the two.

**The total repeated; the rows did not.** Runs A and B both scored 25 Pass, but 10 of the 73 rows
changed verdict between them: rows 7, 15, 25, 32 and 75 passed only in run B, rows 37, 44, 50,
74 and 77 only in run A. The twin's replies vary from run to run, and so does the judge: row 25
got the identical reply in both runs and was failed in A, passed in B. Treat any single row's
verdict, and any category with a handful of rows, as noisy.

Split by where the ground truth came from:

| Ground truth | n | Aug 27 (older prompt) | Oct 13, run A | Oct 13, run B | Oct 13, mean |
|--------------|---|----------------------:|--------------:|--------------:|-------------:|
| `founder-written` (the founder rewrote the reply) | 35 | 0.200 | 0.257 | 0.314 | 0.286 |
| `as-sent` (the founder marked the reply Pass) | 38 | 0.421 | 0.421 | 0.368 | 0.395 |

By category, Oct 13, most failures first. Most categories have one to three rows, so a single
flip moves them a lot:

| Category | Rows | Pass, run A | Pass, run B | Mean pass rate |
|----------|-----:|------------:|------------:|---------------:|
| Refund | 6 | 0 | 0 | 0.00 |
| Bulk/Wholesale | 7 | 2 | 2 | 0.29 |
| Sizing/Band | 7 | 2 | 3 | 0.36 |
| Price | 7 | 3 | 3 | 0.43 |
| Product Recommendation | 6 | 3 | 3 | 0.50 |
| Collab | 3 | 0 | 0 | 0.00 |
| Order Status/Payment | 3 | 0 | 0 | 0.00 |
| Stock/Restock | 3 | 0 | 0 | 0.00 |
| Discount/Offers | 2 | 0 | 0 | 0.00 |
| Payment Method | 2 | 0 | 0 | 0.00 |
| Price/Bestsellers | 2 | 0 | 0 | 0.00 |
| Shipping/Delivery | 2 | 0 | 0 | 0.00 |
| Order Status/Tracking | 2 | 1 | 0 | 0.25 |
| Image/Vision | 4 | 2 | 4 | 0.75 |
| Off-topic | 2 | 1 | 1 | 0.50 |
| Product Info | 2 | 1 | 1 | 0.50 |
| Complaint/Product Mismatch | 1 | 0 | 0 | 0.00 |
| Photos Request | 1 | 0 | 0 | 0.00 |
| Refund/Complaint | 1 | 0 | 0 | 0.00 |
| Order/Purchase Link | 3 | 3 | 2 | 0.83 |
| Order ID Lookup | 1 | 1 | 0 | 0.50 |
| Allergy/Safety | 1 | 1 | 1 | 1.00 |
| Cross-channel | 1 | 1 | 1 | 1.00 |
| Damaged/Wrong Item | 1 | 1 | 1 | 1.00 |
| Feedback/Testimonial | 1 | 1 | 1 | 1.00 |
| Returns | 1 | 1 | 1 | 1.00 |
| Shipping/Courier | 1 | 1 | 1 | 1.00 |

**Judge agreement (Oct 13 calibrate run).** `--mode calibrate` re-scored the recorded answers of
the 35 founder-written rows, 26 of which carry a founder verdict. Today's prompt agreed on
**23 of 26 (88.5%)**, with 3 disagreements: row 32 (founder Partial, judge Fail), and rows 41
and 60 (founder Fail, judge Pass; row 60 is the "Thank you for reaching out" case described
under [Calibrating the judge](#calibrating-the-judge)). Today's prompt can only answer Pass or
Fail, so row 32 can never agree and 25 of 26 is the ceiling. The Aug 27 figure was also 88.5%,
from the older three-way prompt on the same 26 rows. The match is a coincidence, and the two
are not directly comparable. **This is in-sample too:** today's prompt was adopted after a
benchmark against these same labelled rows (commit `31aea62`), so it is a fit, not a held-out
estimate of judge accuracy.

**Failed only for a dropped follow-up question (Oct 13).** Reading each failed row's reply
against its ideal, these rows got every fact right and failed only because the reply left out a
closing question or offer that the ideal had:

- Run A, 13 rows: 1, 4, 5, 6, 7, 25, 28, 32, 40, 41, 64, 65, 66
- Run B, 10 rows: 4, 5, 6, 28, 35, 40, 41, 64, 65, 74

Counting them as passes gives **52.1%** for run A (38/73), **47.9%** for run B (35/73), and a
mean of **50.0%**. On Aug 27 the same adjustment covered eight rows and moved ~31% to ~42%.

- In rows 40, 41, 64, 65 and 66 the missing question is "could you share your order ID?" on a
  refund complaint the twin escalated. On Instagram an escalated reply's text is not sent at
  all (see the caveats below), so for the Instagram rows 64, 65 and 66 the customer never sees
  this text either way.
- Row 25 in run A is a judge slip, not a missing question: the reply did ask which occasion it
  was for, and the identical reply passed in run B.
- Row 15 (run A) and row 39 also lack only a question, but there the question is the whole
  ideal reply, so they are left out. Row 35 in run A also left out the dispatch time, so it is
  left out too.
- The list comes from reading the rows, not from the judge. As on Aug 27, whether a reply that
  states the right facts but doesn't ask the next question is *wrong* is the founder's product
  decision, not the judge's.

**What the Oct 13 number does not cover.** Read these together with the Aug 27 points further
down, which still apply:

- **This scores the reply-writing step (draft_reply_logic, the LangGraph generate node). The
  output guard and routing are covered by unit tests, not by this eval.** One concrete case: on
  Instagram an ESCALATE reply's text is never sent. The customer gets a fixed holding line
  instead, or nothing for a legal or press message, or the allergy line for a safety report
  (`_ig_escalation_reply` in `app.py`). Rows 10, 36, 40, 41, 63, 64, 65, 66, 69 and 73
  were classified ESCALATE in both runs. Six of them (63–66, 69, 73) are Instagram rows, so
  for those the eval grades text no customer receives.
- **Single-turn only: no conversation history.**
- **An internal review on Sept 28 found this judge can pass replies containing invented product
  claims, so a Pass means "matches the ideal", not "contains nothing false".**
- **The judge sees only the reply text, not the classification.** A row whose ideal is a
  routing instruction is graded on wording alone. Row 63's ideal says the request should be
  flagged for the founder's decision. The twin escalated it in both runs, and it still failed.
- **The ground truth is older than today's `brain.md` and today's stock.** The ideals were
  written before the Sept 28 audit fixes, and some now contradict a deliberate rule or the live
  inventory. Reading the rows, these failures follow today's rule where the ideal does not:
  - Rows 30 and 48, and row 46 in run B: the ideal says GS3 is sold out and offers to take a
    number. The live inventory, read right after the runs, listed GS3 in stock (run B's reply
    to row 46 says the same), and `brain.md` now forbids taking numbers for restock alerts.
  - Rows 21 and 54: the ideal ends with "we'll take it from there", a follow-up promise
    `brain.md` now bans on AUTO replies. Row 21's ideal also quotes the ₹749 bulk rate for an
    unspecified "20" without confirming trays, which the finding 3 rule now forbids.
  - Row 60: the twin drafts `brain.md`'s collab line for the founder's approval, without the
    ideal's "within a few hours".

  Row 39 is left out. `brain.md` answers a general refund-timing question with the timings
  alone, but escalates a customer asking about their own refund, and this single message could
  be either. It cuts both ways. In row 31 (both runs) and row 46 (run A) the reply treats GS3 as
  sold out, although the live inventory listed it in stock, and the judge didn't notice because
  the ideal says the same. None of this is applied to the numbers above, and `eval_set.json` is unchanged.
- **How the Oct 13 runs were made.** A plain `python -m eval.harness` run starts from an empty
  scratch database and builds the RAG index in the background, so row 1 is answered without
  RAG (seen in a 5-row smoke test). Both fresh runs therefore used a throwaway wrapper. It waited
  for the 46-chunk index, then called `eval.harness.main` with the same arguments. The calibrate
  run never calls the twin, so it used the plain command line. Each fresh run took about 5
  minutes. It sent about 2.0M DeepSeek input tokens (about 27k per row, almost all of it the
  system prompt) and about 5k output tokens.

### Aug 27, 2026: the first run (older judge prompt)

This run used the Aug 27 judge prompt, which commit `31aea62` replaced on Sept 1. The
agreement figures in this table, and the calibration numbers further down, belong to that older
prompt.

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

#### Three things the Aug 27 number does not mean

Written for the Aug 27 run. All three still apply; points 1 and 2 have Oct 13 numbers above.

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
It happened again on Oct 13. Row 22's reason in run B called the ₹749/tray bulk rate
"incorrect pricing", and row 54's reason in run A called "200+ pairs" (20 trays of 10 pairs)
"potentially incorrect". Both are true.

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

The recorded answers (`actual_answer`) stay exactly as sent, so 7 of them quote prices that have
since changed (trays at ₹699, free shipping above ₹699, older single-pair, duo and trio prices) — on
purpose, since calibration grades those exact texts against the founder's verdicts.

What calibration actually caught, in order. The first three rows are from August, before the
Sept 1 prompt change. The last row is today's prompt, measured on Oct 13:

| Judge + rubric | Agreement with founder (26 labelled rows) |
|----------------|--------------------------------------------|
| `qwen3:8b`, first rubric | 27% |
| `gpt-oss-120b`, first rubric | 54% |
| `gpt-oss-120b`, + completeness rule | **88%** |
| `gpt-oss-120b`, today's Pass/Fail prompt (Oct 13) | 88.5% (23/26; 25/26 is the ceiling, see [Results](#results-and-how-to-read-them)) |

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

## Resuming a run, and reviewing verdicts yourself

The HTTP route scores everything in one request, as before. The command line adds two things:

- **Resuming.** `--run-id NAME` saves each row's progress in `eval/runs/checkpoints.sqlite`,
  one LangGraph thread per row (`NAME:<row id>`). If the run stops (a crash, Ctrl+C, a Groq
  rate limit), run the same command again. Rows that already have a verdict are reused with
  no model call. Rows that ended in `Error` run again. A row that was cut off halfway continues
  from the step where it stopped. A run id keeps its judge and mode, so the same id with
  another `--judge` or `--mode` is refused. Use a new id for that.
- **Review.** `--review` pauses each row the judge scored `Partial`. In `--mode calibrate` it
  also pauses each row where the judge disagrees with your recorded verdict. For each one it
  shows the question, the twin's answer, the ideal answer and the judge's verdict and reason,
  then asks `Pass / Partial / Fail / skip`. The judge's prompt asks for Pass or Fail only, so
  in `fresh` mode a Partial is rare (the Groq run above had none), and a review run may never
  pause. In `calibrate` mode it pauses on every disagreement.
  - Your answer is saved as `founder_verdict`.
  - The judge's verdict stays in `verdict` (and `judge_verdict`), so the pass rate and
    `judge_agreement` still measure the judge.
  - `skip` keeps the judge's verdict and records nothing.
  - If you stop at a prompt, the next `--review` run asks again without a model call.
  - Review only covers rows judged in a `--review` run. A row that already finished without
    it is not reopened.

The full results are written to `eval/runs/NAME.json`. `eval/runs/` is gitignored because it
holds model outputs. Never commit it.

Install the eval requirements once. This adds `langgraph-checkpoint-sqlite`:

```powershell
venv\Scripts\python.exe -m pip install -r eval/requirements.txt
```

Then, in Windows PowerShell from the repo root:

```powershell
# A full scored run. If it stops, run the same line again to continue.
venv\Scripts\python.exe -m eval.harness --run-id oct08

# The same run, pausing each Partial verdict for your own verdict.
venv\Scripts\python.exe -m eval.harness --run-id oct08-review --review

# Calibrate the judge, reviewing only where it disagrees with your recorded verdict.
venv\Scripts\python.exe -m eval.harness --run-id oct08-cal --mode calibrate --review

# A five-row smoke test with the local judge.
venv\Scripts\python.exe -m eval.harness --run-id smoke --judge ollama --limit 5
```

Every row that isn't reused calls the models: DeepSeek for the twin (`fresh` mode only), and
the judge.

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
   someone. It's installed by `run_eval()` when a run actually starts — **never** at module
   import time. Anything outside this package (e.g. app.py) that imports from `eval.*` would
   otherwise get these raisers patched into `app` permanently just by importing, whether or
   not it ever runs an eval — which is exactly what broke production sends in Sep 2026.
   Production code must never import from `eval.*` for this reason.

`harness.py` also blanks `GITHUB_TOKEN` / `GITHUB_REPO` and points `DB_PATH` at a temp file
*before* importing `app`, because importing `app` runs `_restore_db_from_github()` and starts
the hourly backup loop. Without that guard an eval run could pull down and re-push the
production SQLite.

## Files

```
eval/
├── convert.py          # one-time xlsx -> eval_set.json
├── harness.py          # LangGraph: generate -> judge (-> review), scoring, command line
├── api.py              # FastAPI: GET /health, POST /eval/run
├── requirements.txt    # eval-only deps; NOT installed on Render
├── data/eval_set.json  # 73 scorable rows
└── runs/               # resumable-run checkpoints and results; gitignored, never commit
```

Local only. Not deployed, and nothing in `app.py` imports from this directory.
