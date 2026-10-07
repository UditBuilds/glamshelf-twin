"""LangGraph orchestration layer for the GlamShelf Twin Instagram flow.

This module makes the twin's implicit message pipeline EXPLICIT as a graph:

    intake -> generate -> triage -> dispatch_*
       \\ (gate hit)
        `-> END

dispatch_* is dispatch_auto / _draft / _escalate / _lead, or
dispatch_failure when the pipeline itself failed (LLM error, unusable
output) with no escalation signal: the customer still gets the brain's
holding line (audit T1-8). Every message that reaches triage is
dispatched — an empty AUTO or DRAFT+APPROVE reply goes to the founder as
a draft (audit finding 1), as in production.

Every node DELEGATES to the existing production function in app.py.
generate is app.draft_reply_logic itself, called with the arguments the
live handler passes: the brain.md prompt with live inventory, policies and
RAG context, the DeepSeek call and its retry, and the prefilters are the
production code, in production's order. The Instagram/Telegram dispatch
uses the production helpers too. The graph is the routing skeleton;
app.py remains the single source of behavior. Parity with
_process_instagram_event (app.py) is enforced by tests/test_graph_parity.py.

WIRED BEHIND TWIN_USE_LANGGRAPH, OFF BY DEFAULT. With TWIN_USE_LANGGRAPH=1
(exactly "1"), app.py imports this module once at startup
(_load_twin_graph) and _process_instagram_event hands each text DM to
handle_instagram_message(..., gates_done=True). The handler keeps
everything up to and including its photo / media branch: page-echo and
manual-reply detection (HUMAN_UDIT_IG), the is_echo drop, non-text events,
dedup, the pause gate, the human-handling check, photos and other media.
The graph starts at the rate limit and only ever sees text. With the flag
OFF, the default, app.py never imports this module or langgraph and the
legacy handler answers everything. If the import fails, replies stay on
the legacy path. The keyed /healthz shows which path is answering
("reply_path") and any import error ("langgraph_error").

Design notes (full write-ups in the Obsidian vault,
langgraph-glamshelf-twin.md):

  * State is one shared TypedDict (TwinState). Nodes return only the keys
    they produced; LangGraph merges them into the state. total=False means
    every key is optional — a node reads with .get() and must not assume an
    upstream key exists unless the graph topology guarantees it ran.

  * Triage decisions reuse the production category names AUTO /
    DRAFT+APPROVE / ESCALATE (not Creative Triage's AUTO_PUBLISH /
    DRAFT_NEEDS_EDIT / ESCALATE_RISK). The names are a live contract: they
    are the LLM's JSON output schema (build_user_message), brain.md
    Section 5's vocabulary, the Telegram notification headers, and the
    values stored in DB log rows. The PATTERN is what carried over —
    deterministic floors that can only tighten the LLM's call, never
    loosen it.
"""

import json
import traceback
from typing import TypedDict

from langgraph.graph import StateGraph, START, END

# The production module. Nodes call app.<fn> at invoke time (late attribute
# lookup), so test monkeypatches on app apply to the graph automatically.
# When app.py loads this module (flag ON), this returns the running app:
# under gunicorn it is sys.modules["app"], and under `python app.py`
# _load_twin_graph registers __main__ as "app" first. Otherwise it is an
# ordinary import, and the first import of app runs its startup block (DB
# init/restore, RAG model load), the same cost every test module pays.
import app


class TwinState(TypedDict, total=False):
    """Everything the pipeline knows about one inbound Instagram DM.

    Grouped by the node that writes each key. A single flat dict (rather
    than nested per-node objects) keeps conditional-edge routers trivial:
    they read one key, no unwrapping.
    """

    # -- graph input (caller supplies) --
    sender_id: str      # IG-scoped sender id (17-digit FB id)
    text: str           # the customer's message text (non-empty)
    msg_id: str         # Meta `mid` for dedup; "" skips dedup
    timestamp: str      # stringified event timestamp, for log rows
    gates_done: bool    # caller already ran the dedup/pause/human gates (declared,
                        # or LangGraph would drop it from the input)

    # -- intake --
    drop_reason: str    # set => pipeline stops (duplicate/paused/human)
    order_context: str  # _lookup_recent_order line, "" if no match
    history: list       # prior IG exchanges (oldest first), [] if none

    # -- generate (draft_reply_logic's return, or its error) --
    raw_response: str        # LLM output after fence stripping
    llm_classification: str  # its call (prefilters applied), before triage overrides
    reply: str               # drafted customer-facing text
    pipeline_error: str      # "<ExcType>: <msg>" when draft_reply_logic raised

    # -- triage --
    decision: str  # AUTO | DRAFT+APPROVE | ESCALATE | LEAD | FAIL (final)
    tag: str       # the model's optional JSON "tag" (LEAD/SAFETY/LEGAL/PRESS/ORDER/RESTOCK), "" if none
    fallback_escalation: bool  # ESCALATE verdict with unusable LLM output
    failure_detail: str  # the pipeline error / unusable-output text, for dispatch_failure
    guard_note: str  # output-guard rule(s) that held an AUTO reply for approval (audit T1-4)

    # -- dispatch --
    dispatch: dict  # what the dispatch node actually did, for the caller


# ---------------------------------------------------------------------------
# Nodes. Each is a plain function State -> partial State. Side effects
# (DB writes, network sends) live only in intake (dedup persist) and the
# dispatch nodes — generate/triage only read.
# ---------------------------------------------------------------------------


def intake(state: TwinState) -> TwinState:
    """Admission gates + per-customer context, mirroring the top of
    _process_instagram_event after its transport-level checks.

    Gate order is load-bearing and copied exactly: dedup FIRST (a duplicate
    delivery must not re-run the pause/human checks or reload context),
    then the in-memory pause gate, then the DB-backed human-handling net,
    then the LLM rate limit (so paused/human-handled senders never use budget).

    With gates_done, intake starts at the rate limit: _process_instagram_event
    hands a text DM over just before its own rate limit, after running the
    first three gates itself. Repeating dedup would drop the message as its
    own duplicate; repeating the other two would query the DB twice.
    """
    sender_id = state["sender_id"]
    msg_id = state.get("msg_id", "")

    if not state.get("gates_done"):
        if msg_id and msg_id in app._seen_ids:
            print(f"[INSTAGRAM] Skipped: duplicate mid {msg_id}")
            return {"drop_reason": "duplicate_mid"}
        if msg_id:
            app._seen_ids.add(msg_id)
            app._persist_seen_id(msg_id)

        # Not answered, but logged and forwarded to the founder (and maybe the
        # one-time line) — same helper as production (audit findings 8, 19).
        if app._is_paused(sender_id):
            print(f"[PAUSED] Not answering — auto-pause active for IG sender {sender_id}")
            app._ig_unanswered(sender_id, state["text"], state.get("timestamp", ""), "paused")
            return {"drop_reason": "paused"}

        if app._udit_replied_recently_ig(sender_id):
            print(f"[HUMAN_HANDLING_IG] Udit replied to {sender_id} on Instagram recently — not answering")
            app._ig_unanswered(sender_id, state["text"], state.get("timestamp", ""), "human_handling")
            return {"drop_reason": "human_handling"}

    # LLM rate limits (audit T1-3) — same gate and helper as production.
    limit = app._llm_admission("Instagram", sender_id)
    if limit:
        app._ig_rate_limited(sender_id, state["text"], state.get("timestamp", ""), limit)
        return {"drop_reason": f"rate_limited:{limit}"}

    return {
        "order_context": app._lookup_recent_order(sender_id),
        "history": app._load_instagram_history(sender_id),
    }


def generate(state: TwinState) -> TwinState:
    """The reply: app.draft_reply_logic itself, with the arguments the live
    handler passes. That is the brain.md prompt with live inventory,
    policies and RAG context, the DeepSeek call with its retry (an
    unusable answer twice becomes ESCALATE with an empty reply), and both
    prefilters, all in production's order.

    If it raises (brain.md missing, DeepSeek down, the reply budget spent),
    the error is recorded and triage runs anyway, which is what the
    handler's except branch does: a prefilter hit still escalates with an
    empty reply (July 17 decision: the verdict survives regardless of what
    happens downstream), anything else gets the holding line and a founder
    alert (audit T1-8)."""
    try:
        classification, reply, raw = app.draft_reply_logic(
            state["text"], state.get("order_context", ""),
            history=state.get("history"), source="Instagram DM",
        )
    except Exception as e:
        print(f"[INSTAGRAM] Twin pipeline failed: {type(e).__name__}: {e}")
        traceback.print_exc()
        return {"pipeline_error": f"{type(e).__name__}: {e}"}

    return {"raw_response": raw, "llm_classification": classification, "reply": reply}


def triage(state: TwinState) -> TwinState:
    """The decision node: deterministic floors over the reply's call, in the
    order _process_instagram_event applies them after draft_reply_logic.

    Both prefilters are UPGRADE-ONLY — they can force ESCALATE, never
    downgrade it. Thresholds are the founder-confirmed production ones:
    the high-risk phrase list (lawyer / consumer court / police / refund
    karo / social-media threat) and the bulk-commit rule (commit signal
    for >=20 trays, or a committed amount over the Rs.1,500 Hard Money
    Threshold, via resolve_pricing_action). draft_reply_logic has already
    applied and logged both on a drafted reply, so the check here changes
    nothing then. It matters when generate failed: as in the handler's
    except branch, a hit still escalates with an empty reply, and nothing
    extra is logged.

    An empty classification/reply never drops the message. An ESCALATE
    verdict survives it — founder decision (July 17, 2026): an escalation
    verdict must survive regardless of what happens downstream, so an
    ESCALATE with unusable reply text (JSON parse failure, empty reply
    field, or a failed LLM call after a prefilter hit) still escalates;
    dispatch_escalate decides what the customer gets (holding line, safety
    line, or silence for legal/press). Production
    (_process_instagram_event) implements the same rule — parity holds.

    A tester / brand owner / question about the AI service decides LEAD
    (friendly reply, LEAD notice, no pause) unless something more serious
    is going on — see app._ig_is_lead.

    Without an escalation verdict, a pipeline failure or an unusable
    classification routes to FAIL -> dispatch_failure (holding line +
    founder alert, audit T1-8), and an empty AUTO / DRAFT+APPROVE reply
    decides DRAFT+APPROVE -> dispatch_draft: the founder gets it as a draft
    and the customer the handoff line (audit finding 1), as in production.
    """
    classification = state.get("llm_classification", "")
    reply = state.get("reply", "")

    # The prefilters, silently: draft_reply_logic logged any hit already,
    # and the handler's except branch re-checks without a log line.
    if classification != "ESCALATE" and (
        app._escalation_prefilter_hit(state["text"])
        or app._bulk_commit_prefilter_hit(state["text"]) is not None
    ):
        classification = "ESCALATE"

    # Output guard (audit T1-4): an AUTO reply that trips a rule is held
    # for approval — same helper, same founder-notice input (rule 7) and
    # same place as production.
    tag = app._parse_twin_tag(state.get("raw_response", ""))
    guard_note = app._ig_output_guard(
        state["sender_id"], classification, reply,
        founder_notice=app._ig_founder_told(state["text"], classification, reply, tag),
    )
    if guard_note:
        classification = "DRAFT+APPROVE"

    # Testers / brand owners / questions about the AI service: friendly
    # reply, LEAD notice, no pause (audit T1-5) — same rule as production.
    if app._ig_is_lead(state["text"], classification, reply, tag):
        return {
            "decision": "LEAD",
            "reply": reply if classification == "AUTO" else "",
            "tag": tag,
        }

    if not classification or not reply:
        if classification == "ESCALATE":
            # No usable model reply: escalate like any other — the draft
            # stays empty and _ig_escalate decides what the customer gets.
            print("[ESCALATE] Verdict with no usable reply — escalating instead of dropping")
            return {
                "decision": "ESCALATE",
                "reply": "",
                "fallback_escalation": True,
                "tag": tag,
            }
        if state.get("pipeline_error"):
            # draft_reply_logic raised and no prefilter escalated: holding
            # line + founder alert, same as production (audit T1-8).
            return {"decision": "FAIL", "failure_detail": state["pipeline_error"]}
        if classification in ("AUTO", "DRAFT+APPROVE"):
            # Never silence (audit finding 1): an empty reply goes to the
            # founder as a draft, the same way an output-guard hold does,
            # and the customer gets the handoff line (or, inside its
            # window, the one-time acknowledgement) — same as production.
            print(
                f"[INSTAGRAM] Twin returned an empty {classification} reply for "
                f"{state['sender_id']} — sending the message to the founder as a draft"
            )
            return {
                "decision": "DRAFT+APPROVE", "reply": "", "fallback_escalation": False,
                "tag": tag, "guard_note": guard_note,
            }
        print(f"[INSTAGRAM] Unusable model output (classification={classification!r})")
        return {
            "decision": "FAIL",
            "failure_detail": f"unusable model output (classification={classification!r})",
        }

    if classification not in ("AUTO", "DRAFT+APPROVE", "ESCALATE"):
        print(f"[INSTAGRAM] Unknown classification {classification!r}")
        return {
            "decision": "FAIL",
            "failure_detail": f"unusable model output (classification={classification!r})",
        }

    return {
        "decision": classification, "reply": reply, "fallback_escalation": False,
        "tag": tag, "guard_note": guard_note,
    }


def dispatch_auto(state: TwinState) -> TwinState:
    """AUTO: send on Instagram immediately. _send_instagram_reply returns
    (ok, error) — success is only logged as a delivered exchange when the
    Graph API confirmed it; failures keep the text for audit under
    AUTO_FAILED_IG, which history loading excludes."""
    sender_id = state["sender_id"]
    reply = state["reply"]

    sent, send_err = app._send_instagram_reply(sender_id, reply)
    if sent:
        app._log_instagram(sender_id, state["text"], reply, state.get("timestamp", ""))
        print(f"[INSTAGRAM-AUTO] Replied to {sender_id}")
    else:
        app._log_instagram(
            sender_id, state["text"], reply, state.get("timestamp", ""),
            source="AUTO_FAILED_IG",
        )
        print(f"[INSTAGRAM-AUTO] Send FAILED to {sender_id}: {send_err}")
    # Order / restock heads-up to the founder — same helper as production.
    topic = app._ig_fyi_topic(state["text"], state.get("tag", ""))
    if topic:
        app._ig_send_fyi(sender_id, state["text"], reply, topic, sent)
    # 20+ tray question: LEAD notice — same helper as production.
    app._ig_send_bulk_lead(sender_id, state["text"], reply if sent else f"(send FAILED) {reply}")

    return {"dispatch": {"channel": "instagram", "sent": sent, "error": send_err}}


def dispatch_draft(state: TwinState) -> TwinState:
    """DRAFT+APPROVE: the customer gets the handoff line (app._ig_draft_handoff,
    audit T2-14) and the founder a buttoned Telegram approval, plus a LEAD
    notice when a tester's answer is the one waiting (audit finding 4) and
    a bulk LEAD notice for a 20+ tray question; the drafted reply itself
    doesn't reach the customer here. Same calls in the
    same order as production's draft branch. The approval continuation
    (pending_drafts table + /telegram-callback) lives outside the graph —
    see the Obsidian note on why this isn't a LangGraph interrupt()."""
    sender_id = state["sender_id"]
    text = state["text"]
    reply = state["reply"]

    handoff_sent = app._ig_draft_handoff(sender_id, state.get("timestamp", ""))
    # The username comes after the handoff line — same as production.
    username = app._ig_username(sender_id)
    sent_with_buttons = app.send_draft_for_approval(
        customer_number=sender_id,
        customer_name=app._ig_profile_link(username) if username else "",
        customer_message=text,
        reply_text=reply,
        channel="Instagram",
        ig_timestamp=state.get("timestamp", ""),
        guard_note=state.get("guard_note", ""),
    )
    if not sent_with_buttons:
        try:
            app.send_telegram_notification(
                state["decision"], text, reply,
                sender_info=f"Instagram DM — {app._ig_sender_label(sender_id)}",
                channel="Instagram",
            )
        except Exception as tg_err:
            print(
                f"[INSTAGRAM-TG] Fallback notification failed: "
                f"{type(tg_err).__name__}: {tg_err}"
            )
    # A tester whose answer waits for approval is still a LEAD — same
    # helper as production, told whether the handoff line went out now.
    app._ig_lead_draft_notice(sender_id, text, state.get("tag", ""), handoff_sent)
    # A 20+ tray question whose answer waits for approval is still a bulk
    # lead — same helper as production.
    app._ig_send_bulk_lead(sender_id, text, "(waiting for your approval — see the 🟡 draft)")
    app._log_instagram(sender_id, text, None, state.get("timestamp", ""), source="DRAFT_PENDING_IG")
    print(f"[INSTAGRAM-DRAFT] Notified founder for {sender_id} (buttons={sent_with_buttons})")

    return {"dispatch": {"channel": "telegram_draft", "buttons": sent_with_buttons}}


def dispatch_escalate(state: TwinState) -> TwinState:
    """ESCALATE: the customer gets the brain's holding line (or the safety
    line for a reported reaction) unless it's a legal threat or press;
    the founder is paged with a ▶️ Resume button; the thread pauses 4h.
    All of it lives in app._ig_escalate, the helper production calls, so
    the two can't drift (audit T1-5)."""
    result = app._ig_escalate(
        state["sender_id"],
        state["text"],
        state.get("timestamp", ""),
        state.get("reply", ""),
        state.get("tag", ""),
        state.get("fallback_escalation", False),
    )
    return {"dispatch": result}


def dispatch_lead(state: TwinState) -> TwinState:
    """LEAD: a tester / brand owner / question about the AI service gets a
    friendly reply and the founder a LEAD notice — no pause. Same helper
    as production (audit T1-5)."""
    sent = app._ig_lead(
        state["sender_id"],
        state["text"],
        state.get("timestamp", ""),
        state.get("reply", ""),
    )
    return {"dispatch": {"channel": "instagram_lead", "sent": sent}}


def dispatch_failure(state: TwinState) -> TwinState:
    """FAIL: the pipeline itself failed (LLM error or timeout, unusable
    output) with no escalation signal. The customer still gets the brain's
    holding line and the founder a rate-limited alert — the same helper
    production calls, so the two can't drift (audit T1-8)."""
    sent = app._ig_pipeline_failure(
        state["sender_id"],
        state["text"],
        state.get("timestamp", ""),
        state.get("failure_detail", ""),
    )
    return {"dispatch": {"channel": "instagram_failure", "holding_line_sent": sent}}


# ---------------------------------------------------------------------------
# Routers. Conditional-edge functions return a label; the path map at
# add_conditional_edges translates labels to nodes (or END). Keeping
# routers as pure one-key reads is what makes the graph diagram honest:
# every branch in the pipeline is visible in build_graph(), none hide
# inside node bodies.
# ---------------------------------------------------------------------------


def _route_after_intake(state: TwinState) -> str:
    return "drop" if state.get("drop_reason") else "continue"


def _route_after_triage(state: TwinState) -> str:
    # triage always decides; "drop" (-> END) only guards an unexpected value.
    return {
        "AUTO": "auto",
        "DRAFT+APPROVE": "draft",
        "ESCALATE": "escalate",
        "FAIL": "failure",
        "LEAD": "lead",
    }.get(state.get("decision", ""), "drop")


def build_graph():
    """Wire and compile the pipeline. No checkpointer: every invocation is
    a single synchronous pass, and the only human-in-the-loop step
    (draft approval) is persisted by the existing pending_drafts table,
    not by graph state."""
    g = StateGraph(TwinState)

    g.add_node("intake", intake)
    g.add_node("generate", generate)
    g.add_node("triage", triage)
    g.add_node("dispatch_auto", dispatch_auto)
    g.add_node("dispatch_draft", dispatch_draft)
    g.add_node("dispatch_escalate", dispatch_escalate)
    g.add_node("dispatch_failure", dispatch_failure)
    g.add_node("dispatch_lead", dispatch_lead)

    g.add_edge(START, "intake")
    g.add_conditional_edges(
        "intake", _route_after_intake,
        {"continue": "generate", "drop": END},
    )
    # A failed generate still reaches triage: the prefilters can escalate
    # without a model reply.
    g.add_edge("generate", "triage")
    g.add_conditional_edges(
        "triage", _route_after_triage,
        {
            "auto": "dispatch_auto",
            "draft": "dispatch_draft",
            "escalate": "dispatch_escalate",
            "failure": "dispatch_failure",
            "lead": "dispatch_lead",
            "drop": END,
        },
    )
    g.add_edge("dispatch_auto", END)
    g.add_edge("dispatch_draft", END)
    g.add_edge("dispatch_escalate", END)
    g.add_edge("dispatch_failure", END)
    g.add_edge("dispatch_lead", END)

    return g.compile()


twin_graph = build_graph()


def handle_instagram_message(
    sender_id: str, text: str, msg_id: str = "", timestamp: str = "",
    *, gates_done: bool = False,
) -> dict | None:
    """Graph-driven equivalent of _process_instagram_event's body AFTER its
    transport-level checks (page-echo / HUMAN_UDIT_IG detection, is_echo
    drop, empty text/sender skips). Callers pass a validated customer DM;
    `timestamp` is the already-stringified event timestamp.

    gates_done=True is how _process_instagram_event calls it when
    TWIN_USE_LANGGRAPH=1: it has already run the dedup, pause and
    human-handling gates and handled photos and other media, so intake
    starts at the rate limit. The default runs every gate.

    Never raises — mirrors the existing handler's absorb-into-logs
    contract so one bad message can't break a webhook batch. Returns the
    final graph state, or None if the pipeline threw.
    """
    try:
        return twin_graph.invoke({
            "sender_id": sender_id, "text": text, "msg_id": msg_id,
            "timestamp": timestamp, "gates_done": gates_done,
        })
    except Exception as e:
        # Production's handler catch-all, log line included: alert only,
        # since a reply may or may not have gone out before the error.
        print(f"[INSTAGRAM] Event handler error: {type(e).__name__}: {e}")
        traceback.print_exc()
        app._alert_send_failure(
            "Instagram", f"{type(e).__name__}: {e}", sender_id or "(unknown)",
            kind="pipeline", holding_sent=None,
        )
        return None
