"""Orchestrator loop (ARCHITECTURE §6.2): append-only epochs, tools through the gateway,
submit_answer -> triple check -> verified / revised / downgraded answer."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from .. import db
from ..config import get_settings
from ..context import builder, epochs, snapshots
from ..dataplane import render
from ..llm import LLMGateway, RefusalError, gateway
from ..memory import ltm, stm
from ..tools import gateway as tool_gateway, router
from ..tools.impl import ToolContext
from . import verify

log = logging.getLogger("itsm_agent")


@dataclass
class Outcome:
    job_id: str
    status: str
    banner: str | None
    answer_md: str
    pending_approvals: list[str]


def append_event(session_id: str, event_type: str, payload: dict, job_id: str | None = None) -> int:
    with db.tx() as c:
        c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"events:{session_id}",))
        seq = c.execute("SELECT coalesce(max(seq), 0) + 1 AS n FROM session_events WHERE session_id = %s", (session_id,)).fetchone()["n"]
        row = c.execute("""INSERT INTO session_events (session_id, seq, event_type, job_id, payload) VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                        (session_id, seq, event_type, job_id, db.j(payload))).fetchone()
    return row["id"]


def new_session(user_id: str, title: str | None = None) -> str:
    u = db.q1("SELECT time_zone FROM users WHERE id = %s", (user_id,))
    sid = str(db.q1("INSERT INTO sessions (user_id, title) VALUES (%s,%s) RETURNING id", (user_id, title))["id"])
    stm.ensure_session_state(sid, u["time_zone"])
    return sid


class Orchestrator:
    def __init__(self, llm: LLMGateway | None = None):
        self.llm = llm or gateway()
        self.s = get_settings()

    # ----------------------------------------------------------------------------- public
    def ask(self, session_id: str, text: str) -> Outcome:
        session = db.q1("SELECT * FROM sessions WHERE id = %s", (session_id,))
        user = db.q1("SELECT * FROM users WHERE id = %s", (session["user_id"],))
        ev = append_event(session_id, "user_message", {"text": text})
        job = db.q1("""INSERT INTO jobs (session_id, user_id, request_event, status, ledger) VALUES (%s,%s,%s,'running',%s) RETURNING *""",
                    (session_id, user["id"], ev, db.j({"question": text})))
        return self._run(job, session, user, text, ev)

    # ----------------------------------------------------------------------------- loop
    def resume(self, job_id: str) -> Outcome:
        """Continue a job after a crash from durable state: the open epoch's last snapshot is the exact request
        last sent; the logged response/tool results after it are re-appended (append-only, nothing re-sent twice).
        If the response was logged but its tool results were not, the response is dropped and the same request
        is sent again (tools only run if the model asks again; writes are idempotent proposals)."""
        job = db.q1("SELECT * FROM jobs WHERE id = %s", (job_id,))
        if job["status"] not in ("running", "queued", "paused"):
            raise ValueError(f"job {job_id} is {job['status']}")
        session = db.q1("SELECT * FROM sessions WHERE id = %s", (job["session_id"],))
        user = db.q1("SELECT * FROM users WHERE id = %s", (job["user_id"],))
        question = db.q1("SELECT payload FROM session_events WHERE id = %s", (job["request_event"],))["payload"]["text"]
        epoch = epochs.current_epoch(job_id)
        if epoch is None:
            return self._run(job, session, user, question, job["request_event"])
        snap = db.q1("""SELECT * FROM context_snapshots WHERE epoch_id = %s ORDER BY turn DESC LIMIT 1""", (epoch["id"],))
        if snap is None:
            epochs.close_epoch(str(epoch["id"]), "error_spiral", epochs.build_handoff(job_id, question), 0, {})
            return self._run(job, session, user, question, job["request_event"])
        request = snapshots.replay_request(str(snap["id"]))
        blocks = [{**m, "text": snapshots.get_blob(m["content_hash"])} for m in snap["manifest"]
                  if m["layer"] not in ("L8", "-")]
        base = builder.EpochBase(tools=request["tools"], system=request["system"], first_message=request["messages"][0],
                                 blocks=blocks, tokens=sum(b["tokens"] for b in blocks))
        messages = request["messages"]
        turn = snap["turn"]
        events = {e["event_type"]: e["payload"] for e in db.q(
            """SELECT event_type, payload FROM session_events WHERE job_id = %s AND event_type IN ('model_response','tool_result')
               AND (payload->>'epoch')::int = %s AND (payload->>'turn')::int = %s""", (job_id, epoch["seq"], turn))}
        if "model_response" in events and "tool_result" in events:
            messages = messages + [{"role": "assistant", "content": events["model_response"]["content"]},
                                   {"role": "user", "content": events["tool_result"]["results"]}]
        else:
            turn -= 1                      # re-send the identical request for this turn
        return self._run(job, session, user, question, job["request_event"],
                         resume=(epoch, base, messages, turn, list(epoch["tool_names"])))

    def _run(self, job: dict, session: dict, user: dict, question: str, user_event: int, resume: tuple | None = None) -> Outcome:
        job_id, session_id = str(job["id"]), str(session["id"])
        ctx = ToolContext(job_id=job_id, session_id=session_id, user_id=str(user["id"]), user_tz=user["time_zone"],
                          user_turn_event_id=user_event)
        model = self.llm.role_params("orchestrator")["model"]
        tools = router.select("orchestrator", question)
        handoff = None
        revisions = db.q1("SELECT count(*) AS n FROM answers WHERE job_id = %s", (job_id,))["n"]
        nudges, total_turns = 0, 0
        usage_epoch: dict = {}
        while True:
            if resume:
                epoch, base, messages, turn, tools = resume
                resume = None
                ctx.epoch_id = str(epoch["id"])
            else:
                epoch = epochs.open_epoch(job_id, model, tools)
                ctx.epoch_id = str(epoch["id"])
                base = builder.build_epoch_base(job, session, str(user["id"]), tools, question, handoff)
                messages = [base.first_message]
                append_event(session_id, "system_note", {"epoch": epoch["seq"], "tools": tools}, job_id)
                turn = 0
            reset = None
            while reset is None:
                turn += 1
                total_turns += 1
                if total_turns > 120:
                    return self._fail(job_id, session_id, "turn limit reached without a verified answer")
                request = {"model": model, "system": base.system, "tools": base.tools, "messages": messages}
                snap = snapshots.save_snapshot(job_id, ctx.epoch_id, "orchestrator", turn, request,
                                               builder.turn_blocks(base, messages), base.tokens + builder.working_set_tokens(messages))
                try:
                    res = self.llm.call("orchestrator", system=base.system, messages=messages, tools=base.tools,
                                        job_id=job_id, snapshot_id=snap)
                except RefusalError as e:
                    return self._fail(job_id, session_id, f"The model declined this request ({e}).")
                for k in ("input_tokens", "output_tokens", "cache_read_input_tokens"):
                    usage_epoch[k] = usage_epoch.get(k, 0) + (res.usage.get(k) or 0)
                if res.stop_reason == "max_tokens":
                    reset = "budget"          # don't append a truncated turn; continue in a fresh epoch
                    break
                messages.append({"role": "assistant", "content": res.content})
                append_event(session_id, "model_response", {"epoch": epoch["seq"], "turn": turn, "content": res.content,
                                                            "stop_reason": res.stop_reason, "model": res.model}, job_id)
                if res.stop_reason == "pause_turn":
                    continue
                if not res.tool_uses:
                    nudges += 1
                    if nudges > 2:
                        return self._fail(job_id, session_id, "The agent stopped without submitting an answer.")
                    messages.append({"role": "user", "content": "Call submit_answer with your answer (or explain what blocks you via submit_answer caveats)."})
                    continue
                results, outcome = [], None
                for tu in res.tool_uses:
                    ctx.signals.clear()
                    text, is_err = tool_gateway.call(ctx, tu["name"], tu.get("input") or {})
                    if "submit_answer" in ctx.signals and not is_err:
                        outcome, text, is_err = self._check_answer(job_id, session_id, ctx.signals["submit_answer"], question, revisions)
                        revisions += 1
                    if "request_tools" in ctx.signals:
                        extra = router.find_for_capability("orchestrator", ctx.signals["request_tools"], tools)
                        if extra:
                            tools = sorted(set(tools) | set(extra))
                            reset = "tool_set_change"
                            text = json.dumps({"status": "granted", "tools": extra, "note": "They are available in your next turn."})
                        else:
                            text, is_err = "No enabled tool matches that capability.", True
                    results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": text, **({"is_error": True} if is_err else {})})
                messages.append({"role": "user", "content": results})
                append_event(session_id, "tool_result", {"epoch": epoch["seq"], "turn": turn, "results": results}, job_id)
                if outcome is not None:
                    epochs.close_epoch(ctx.epoch_id, "job_end", None, turn, usage_epoch)
                    return outcome
                if reset is None and builder.working_set_tokens(messages) > self.s.working_set_budget_tokens:
                    reset = "budget"
                if reset is None and turn >= self.s.epoch_turn_cap:
                    reset = "turn_cap"
            handoff = epochs.build_handoff(job_id, question)
            problems = epochs.validate_handoff(job_id, handoff)
            if problems:
                log.warning("handoff validation: %s", problems)
            epochs.close_epoch(ctx.epoch_id, reset, handoff, turn, usage_epoch)
            append_event(session_id, "epoch_reset", {"trigger": reset, "handoff_problems": problems}, job_id)
            usage_epoch = {}

    # ----------------------------------------------------------------------------- answers
    def _check_answer(self, job_id: str, session_id: str, draft: dict, question: str, revision: int):
        """Returns (Outcome|None, tool_result_text, is_error)."""
        d = verify.deterministic(job_id, draft, question)
        reports = [d]
        if d.passed:
            v = verify.independent(job_id, draft, question, self.llm)
            reports.append(v)
        passed = all(r.passed for r in reports)
        if passed:
            banner = self._banner(job_id)
            md = render.expand(job_id, draft["narrative_md"]) + render.footer(job_id, banner)
            if draft["caveats"]:
                md += "\n\n" + "\n".join(f"- {c}" for c in draft["caveats"])
            aid = verify.persist(job_id, revision, draft, reports, md, "verified", banner)
            self._after_answer(job_id, session_id, question, md, draft)
            out = Outcome(job_id, "completed", banner, md, self._pending(job_id))
            db.q("UPDATE jobs SET status = %s, banner = %s, completed_at = now() WHERE id = %s",
                 ("awaiting_approval" if out.pending_approvals else "completed", banner, job_id))
            return out, json.dumps({"status": "verified", "answer_id": aid}), False
        failures = [f for r in reports for f in r.failures]
        if revision >= self.s.max_revisions:
            md = self._downgraded(job_id, draft, failures)
            verify.persist(job_id, revision, draft, reports, md, "downgraded", "partially_verified")
            db.q("UPDATE jobs SET status = 'completed', banner = 'partially_verified', completed_at = now() WHERE id = %s", (job_id,))
            return Outcome(job_id, "completed", "partially_verified", md, self._pending(job_id)), json.dumps({"status": "downgraded"}), False
        verify.persist(job_id, revision, draft, reports, None, "failed", None)
        return None, "Verification failed. Fix exactly these and call submit_answer again:\n- " + "\n- ".join(failures), True

    def _banner(self, job_id: str) -> str:
        weak = db.q1("""SELECT count(*) AS n FROM result_sets WHERE job_id = %s AND kind = 'fetch' AND completeness_status <> 'proven'""", (job_id,))["n"]
        review = db.q1("SELECT count(*) AS n FROM record_annotations WHERE job_id = %s AND is_final AND needs_human_review", (job_id,))["n"]
        return "verified" if weak == 0 and review == 0 else "verified_with_caveats"

    def _downgraded(self, job_id: str, draft: dict, failures: list[str]) -> str:
        ok = [c for c in draft["claims"] if not any(c["claim_id"] in f for f in failures)]
        lines = ["**Partially verified answer.** Only the statements below passed verification."]
        lines += [f"- {c['text']}" + (f": {c['value']:g}" if c.get("value") is not None else "") for c in ok]
        lines += ["", "Could not verify:"] + [f"- {f}" for f in failures]
        return "\n".join(lines) + render.footer(job_id, "partially_verified")

    def _after_answer(self, job_id: str, session_id: str, question: str, md: str, draft: dict) -> None:
        headline = md.split("\n")[0][:300]
        stm.update_summary(session_id, question, headline)
        turn = db.q1("SELECT count(*) AS n FROM jobs WHERE session_id = %s", (session_id,))["n"]
        for lst in db.q("SELECT list_key, row_count, spec FROM rendered_lists WHERE job_id = %s", (job_id,)):
            from ..dataplane import analyzer
            _, rows, _ = analyzer.list_rows(job_id, lst["list_key"], 50)
            stm.add_referent(session_id, job_id, f"{lst['list_key']} ({lst['row_count']} rows) for: {question[:120]}", "list",
                             lst["list_key"], turn, {str(i + 1): f"{r['_sys']}:{r['_id']}" for i, r in enumerate(rows)})
        for f in db.q("SELECT fact_key, description FROM facts WHERE job_id = %s", (job_id,)):
            stm.add_referent(session_id, job_id, f"{f['fact_key']}: {f['description'][:120]}", "fact", f["fact_key"], turn)
        facts = sorted({f"fact:{r}" for c in draft["claims"] for r in c["refs"] if r.startswith("F-")})
        if facts:
            try:
                ltm.propose(job_id, session_id, str(db.q1("SELECT user_id FROM jobs WHERE id = %s", (job_id,))["user_id"]),
                            "episodic", f"job.{job_id[:8]}", f"Q: {question[:300]} A: {headline}", facts[:5], "org")
            except ltm.MemoryRejected:
                pass

    def _pending(self, job_id: str) -> list[str]:
        return [str(r["id"]) for r in db.q("SELECT id FROM actions WHERE job_id = %s AND status = 'pending_approval'", (job_id,))]

    def _fail(self, job_id: str, session_id: str, reason: str) -> Outcome:
        db.q("UPDATE jobs SET status = 'failed', completed_at = now() WHERE id = %s", (job_id,))
        append_event(session_id, "error", {"reason": reason}, job_id)
        return Outcome(job_id, "failed", None, reason, [])
