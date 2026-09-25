"""LangGraph orchestration layer for the GlamShelf Twin Instagram flow.

This module makes the twin's implicit message pipeline EXPLICIT as a graph:

    intake -> retrieve -> hydrate -> generate -> triage -> dispatch_*
       \\ (gate hit)                                 \\ (deliberately empty reply)
        `-> END                                       `-> END

dispatch_* is dispatch_auto / _draft / _escalate / _lead, or
dispatch_failure when the pipeline itself failed (LLM error, unusable
output) with no escalation signal: the customer still gets the brain's
holding line (audit T1-8).

Every node DELEGATES to the existing production function in app.py — the
RAG layer, the brain.md prompt assembly, the DeepSeek call, the prefilters,
and the Instagram/Telegram dispatch are not rebuilt here. The graph is the
routing skeleton; app.py remains the single source of behavior. Parity with
_process_instagram_event (app.py) is enforced by tests/test_graph_parity.py.

NOT YET WIRED INTO PRODUCTION. app.py does not import this module. To swap
it in, _process_instagram_event would keep its transport-level steps 1-2
(page-echo / HUMAN_UDIT_IG detection, is_echo drop, empty-text/sender
checks) and then call handle_instagram_message() instead of the rest of its
body. Until then the graph is exercised only by the parity test suite.

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

  * One deliberate ordering difference from draft_reply_logic: there the
    prefilters are COMPUTED before the LLM call and APPLIED after; here
    both happen in the triage node (after generate). Both prefilters are
    pure functions of the message text plus one env flag read per call,
    so the result is identical — moving them keeps every classification
    decision in a single node instead of smearing triage across two.
"""

import json
import traceback
from typing import TypedDict

from langgraph.graph import StateGraph, START, END

# The production module. Nodes call app.<fn> at invoke time (late attribute
# lookup), so test monkeypatches on app apply to the graph automatically.
# Importing app runs its startup block (DB init/restore, RAG model load) —
# same cost the existing test suite already pays.
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

    # -- intake --
    drop_reason: str    # set => pipeline stops (duplicate/paused/human)
    order_context: str  # _lookup_recent_order line, "" if no match
    history: list       # prior IG exchanges (oldest first), [] if none

    # -- retrieve --
    retrieved_context: str  # [RETRIEVED CONTEXT] block, "" on gate miss

    # -- hydrate --
    system_prompt: str  # brain.md + live inventory + policies + RAG

    # -- generate --
    raw_response: str        # LLM output after fence stripping
    llm_classification: str  # model's own call, before triage overrides
    reply: str               # drafted customer-facing text

    # -- triage --
    decision: str  # AUTO | DRAFT+APPROVE | ESCALATE | LEAD | FAIL | DROP (final)
    tag: str       # the model's optional JSON "tag" (LEAD/SAFETY/LEGAL/PRESS/ORDER/RESTOCK), "" if none
    fallback_escalation: bool  # ESCALATE verdict with unusable LLM output
    pipeline_error: str  # hydrate/generate failure detail (routing + audit)
    failure_detail: str  # "<ExcType>: <msg>" / unusable-output text, for dispatch_failure

    # -- dispatch --
    dispatch: dict  # what the dispatch node actually did, for the caller


# ---------------------------------------------------------------------------
# Nodes. Each is a plain function State -> partial State. Side effects
# (DB writes, network sends) live only in intake (dedup persist) and the
# dispatch nodes — retrieve/hydrate/generate/triage only read.
# ---------------------------------------------------------------------------


def intake(state: TwinState) -> TwinState:
    """Admission gates + per-customer context, mirroring the top of
    _process_instagram_event after its transport-level checks.

    Gate order is load-bearing and copied exactly: dedup FIRST (a duplicate
    delivery must not re-run the pause/human checks or reload context),
    then the in-memory pause gate, then the DB-backed human-handling net,
    then the LLM rate limit (so paused/human-handled senders never use budget).
    """
    sender_id = state["sender_id"]
    msg_id = state.get("msg_id", "")

    if msg_id and msg_id in app._seen_ids:
        print(f"[INSTAGRAM] Skipped: duplicate mid {msg_id}")
        return {"drop_reason": "duplicate_mid"}
    if msg_id:
        app._seen_ids.add(msg_id)
        app._persist_seen_id(msg_id)

    if app._is_paused(sender_id):
        print(f"[PAUSED] Skipping reply — auto-pause active for IG sender {sender_id}")
        return {"drop_reason": "paused"}

    if app._udit_replied_recently_ig(sender_id):
        print(f"[HUMAN_HANDLING_IG] Udit replied to {sender_id} on Instagram recently — skipping")
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


def retrieve(state: TwinState) -> TwinState:
    """RAG retrieval as its own node. _rag_retrieve never raises and
    returns "" on any miss, so this node cannot fail the graph."""
    return {"retrieved_context": app._rag_retrieve(state["text"])}


def hydrate(state: TwinState) -> TwinState:
    """Assemble the system prompt exactly as draft_reply_logic does:
    live inventory PREPENDED (stock visible at the very top), brain.md,
    then live policies and RAG context APPENDED (brain rules keep prompt
    priority). A failure (e.g. brain.md missing) no longer aborts the
    graph: it routes straight to triage, where a deterministic escalation
    verdict can still dispatch (July 17 decision — the verdict survives
    regardless of what happens downstream). Without a prefilter hit,
    triage drops the message, matching the production handler's
    catch-and-return.
    """
    try:
        if not app.BRAIN_FILE.exists():
            raise FileNotFoundError(f"brain file not found at {app.BRAIN_FILE}")

        brain = app._load_brain_cached()

        live_stock = app.get_live_inventory()
        if live_stock:
            brain = live_stock + "\n\n" + brain

        live_policies = app.get_live_policies()
        if live_policies:
            brain = brain + "\n\n" + live_policies

        retrieved = state.get("retrieved_context", "")
        if retrieved:
            brain = brain + "\n\n" + retrieved
    except Exception as e:
        print(f"[GRAPH] Twin pipeline failed in hydrate: {type(e).__name__}: {e}")
        traceback.print_exc()
        return {
            "pipeline_error": f"hydrate: {type(e).__name__}: {e}",
            "failure_detail": f"{type(e).__name__}: {e}",
        }

    return {"system_prompt": brain}


def generate(state: TwinState) -> TwinState:
    """LLM call + JSON parse. Parse failure — and now the LLM call itself
    failing — leaves classification/reply empty; triage decides whether
    that drops (no escalation signal) or dispatches the fallback
    escalation (deterministic verdict present)."""
    try:
        raw = app.ask_claude(
            state["system_prompt"],
            state["text"],
            state.get("order_context", ""),
            history=state.get("history"),
            source="Instagram DM",
        )
    except Exception as e:
        print(f"[GRAPH] Twin pipeline failed in generate: {type(e).__name__}: {e}")
        traceback.print_exc()
        return {
            "pipeline_error": f"generate: {type(e).__name__}: {e}",
            "failure_detail": f"{type(e).__name__}: {e}",
            "raw_response": "",
            "llm_classification": "",
            "reply": "",
        }

    classification, reply = app._parse_twin_reply(raw)

    # Same retry-then-escalate as draft_reply_logic. Kept in step with it
    # deliberately: test_graph_parity asserts the two paths agree, and an
    # unparseable 200 must not drop a customer silently on either.
    if not classification and not reply:
        print("[TWIN] Unusable model output — retrying the call once before falling back")
        try:
            retry_raw = app.ask_claude(
                state["system_prompt"],
                state["text"],
                state.get("order_context", ""),
                history=state.get("history"),
                source="Instagram DM",
            )
        except Exception as exc:  # noqa: BLE001 - the fallback below is the whole point
            print(f"[TWIN] Retry call failed: {type(exc).__name__}: {exc}")
            retry_raw = ""
        if retry_raw:
            raw = retry_raw
            classification, reply = app._parse_twin_reply(raw)

        if not classification and not reply:
            # ESCALATE with an EMPTY reply on purpose — triage's
            # `if not classification or not reply:` gate is what sets
            # fallback_escalation=True, and that flag is what actually
            # ships the holding reply to the customer.
            print(
                "[TWIN] Retry also unusable — escalating so the founder is "
                "notified and the customer gets the holding reply"
            )
            classification = "ESCALATE"
            reply = ""

    return {"raw_response": raw, "llm_classification": classification, "reply": reply}


def triage(state: TwinState) -> TwinState:
    """The decision node: deterministic floors over the LLM's call.

    Both prefilters are UPGRADE-ONLY — they can force ESCALATE, never
    downgrade it. Thresholds are the founder-confirmed production ones:
    the high-risk phrase list (lawyer / consumer court / police / refund
    karo / social-media threat) and the bulk-commit rule (commit signal
    for >=20 trays, or a committed amount over the Rs.1,500 Hard Money
    Threshold, via resolve_pricing_action).

    Empty classification/reply drops with no dispatch — EXCEPT when the
    verdict is ESCALATE. Founder decision (July 17, 2026): an escalation
    verdict must survive regardless of what happens downstream, so an
    ESCALATE with unusable reply text (JSON parse failure, empty reply
    field, or a failed LLM call after a prefilter hit) still escalates
    instead of dropping; dispatch_escalate decides what the customer gets
    (holding line, safety line, or silence for legal/press). Production
    (_process_instagram_event) implements the same rule — parity holds.

    A tester / brand owner / question about the AI service decides LEAD
    (friendly reply, LEAD notice, no pause) unless something more serious
    is going on — see app._ig_is_lead.

    Without an escalation verdict, a pipeline failure or an unusable
    classification routes to FAIL -> dispatch_failure (holding line +
    founder alert, audit T1-8); only a deliberately empty AUTO / DRAFT
    reply drops.
    """
    classification = state.get("llm_classification", "")
    reply = state.get("reply", "")

    prefilter_phrase = app._escalation_prefilter_hit(state["text"])
    bulk_commit_qty = app._bulk_commit_prefilter_hit(state["text"])

    if prefilter_phrase and classification != "ESCALATE":
        print(
            f"[PREFILTER] Forcing ESCALATE (was {classification or 'unparsed'!r}) — "
            f"matched high-risk phrase {prefilter_phrase!r}"
        )
        classification = "ESCALATE"

    if bulk_commit_qty is not None and classification != "ESCALATE":
        print(
            f"[PREFILTER] Forcing ESCALATE (was {classification or 'unparsed'!r}) — "
            f"bulk commit signal for {bulk_commit_qty} trays "
            f"(Rule 3b-i / Hard Money Threshold, resolve_pricing_action)"
        )
        classification = "ESCALATE"

    # Testers / brand owners / questions about the AI service: friendly
    # reply, LEAD notice, no pause (audit T1-5) — same rule as production.
    tag = app._parse_twin_tag(state.get("raw_response", ""))
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
            # hydrate/generate raised and no prefilter escalated: holding
            # line + founder alert, same as production (audit T1-8).
            return {"decision": "FAIL", "failure_detail": state.get("failure_detail", "")}
        if classification in ("AUTO", "DRAFT+APPROVE"):
            # A deliberately empty reply (brain.md's stay-silent rule).
            print(
                f"[INSTAGRAM] Twin returned empty result "
                f"(classification={classification!r}, reply_len={len(reply)}); not sending"
            )
            return {"decision": "DROP"}
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

    return {"decision": classification, "reply": reply, "fallback_escalation": False, "tag": tag}


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

    return {"dispatch": {"channel": "instagram", "sent": sent, "error": send_err}}


def dispatch_draft(state: TwinState) -> TwinState:
    """DRAFT+APPROVE: buttoned Telegram approval; nothing reaches the
    customer here. The approval continuation (pending_drafts table +
    /telegram-callback) lives outside the graph — see the Obsidian note
    on why this isn't a LangGraph interrupt()."""
    sender_id = state["sender_id"]
    text = state["text"]
    reply = state["reply"]

    sent_with_buttons = app.send_draft_for_approval(
        customer_number=sender_id,
        customer_name="",
        customer_message=text,
        reply_text=reply,
        channel="Instagram",
        ig_timestamp=state.get("timestamp", ""),
    )
    if not sent_with_buttons:
        try:
            app.send_telegram_notification(
                state["decision"], text, reply,
                sender_info=f"Instagram DM — sender {sender_id}", channel="Instagram",
            )
        except Exception as tg_err:
            print(
                f"[INSTAGRAM-TG] Fallback notification failed: "
                f"{type(tg_err).__name__}: {tg_err}"
            )
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


def _route_after_hydrate(state: TwinState) -> str:
    # A hydrate failure skips the LLM call entirely but still reaches
    # triage: the deterministic prefilters can escalate without a prompt.
    return "error" if state.get("pipeline_error") else "generate"


def _route_after_triage(state: TwinState) -> str:
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
    g.add_node("retrieve", retrieve)
    g.add_node("hydrate", hydrate)
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
        {"continue": "retrieve", "drop": END},
    )
    g.add_edge("retrieve", "hydrate")
    g.add_conditional_edges(
        "hydrate", _route_after_hydrate,
        {"generate": "generate", "error": "triage"},
    )
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
    sender_id: str, text: str, msg_id: str = "", timestamp: str = ""
) -> dict | None:
    """Graph-driven equivalent of _process_instagram_event's body AFTER its
    transport-level checks (page-echo / HUMAN_UDIT_IG detection, is_echo
    drop, empty text/sender skips). Callers pass a validated customer DM;
    `timestamp` is the already-stringified event timestamp.

    Never raises — mirrors the existing handler's absorb-into-logs
    contract so one bad message can't break a webhook batch. Returns the
    final graph state, or None if the pipeline threw.
    """
    try:
        return twin_graph.invoke(
            {"sender_id": sender_id, "text": text, "msg_id": msg_id, "timestamp": timestamp}
        )
    except Exception as e:
        print(f"[GRAPH] Twin pipeline failed: {type(e).__name__}: {e}")
        traceback.print_exc()
        # Mirrors production's handler catch-all: alert only, since a
        # reply may or may not have gone out before the error.
        app._alert_send_failure(
            "Instagram", f"{type(e).__name__}: {e}", sender_id or "(unknown)",
            kind="pipeline", holding_sent=None,
        )
        return None
