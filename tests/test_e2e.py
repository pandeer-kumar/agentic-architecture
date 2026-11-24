"""End-to-end through the real orchestrator, LLM gateway (SDK -> fake Claude API), tools, DB and verification."""
from itsm_agent import db
from itsm_agent.harness.orchestrator import Orchestrator
from itsm_agent.tools import executor

Q = "Which network changes were done in the last 24 hours without CAB approval?"


def test_cab_question_exact_and_verified(session, brain, vendors):
    out = Orchestrator().ask(session, Q)
    t = vendors.ds.truth
    assert out.status == "completed" and out.banner in ("verified", "verified_with_caveats")
    assert f"there were {t['network_changes']} network changes" in out.answer_md
    assert f"{t['network_without_cab']} of them have no approved CAB" in out.answer_md
    ans = db.q1("SELECT * FROM answers WHERE job_id = %s AND status = 'verified'", (out.job_id,))
    layers = {r["layer"] for r in db.q("SELECT layer FROM verifications WHERE answer_id = %s", (ans["id"],))}
    assert layers >= {"deterministic", "verifier"}
    snaps = db.q1("SELECT count(*) AS n FROM context_snapshots WHERE job_id = %s", (out.job_id,))["n"]
    calls = db.q1("SELECT count(*) AS n FROM llm_calls WHERE job_id = %s AND agent_role = 'orchestrator'", (out.job_id,))["n"]
    assert snaps == calls > 5                                  # every orchestrator request snapshotted


def test_wrong_number_is_caught_and_revised(session, brain, vendors):
    brain.orch_faults = {"wrong_number_first"}
    out = Orchestrator().ask(session, Q)
    revs = db.q("SELECT revision, status FROM answers WHERE job_id = %s ORDER BY revision", (out.job_id,))
    assert [r["status"] for r in revs] == ["failed", "verified"]
    fails = db.q("""SELECT v.check_id FROM verifications v JOIN answers a ON a.id = v.answer_id
                    WHERE a.job_id = %s AND v.verdict = 'fail'""", (out.job_id,))
    assert any(f["check_id"] == "D3" for f in fails)
    assert f"{vendors.ds.truth['network_without_cab']} of them" in out.answer_md


def test_followup_write_needs_approval_and_executes_once(session, brain, vendors, approver):
    Orchestrator().ask(session, Q)
    out = Orchestrator().ask(session, "Add a comment on those changes asking for the CAB approval")
    assert out.status == "completed" and len(out.pending_approvals) == 3
    assert "awaiting approval" in out.answer_md
    before = len(vendors.writes)
    for aid in out.pending_approvals:
        executor.decide(aid, approver, "approve")
    done = executor.run_approved()                             # executes every approved action (global worker)
    mine = {d["action_id"]: d["status"] for d in done if d["action_id"] in out.pending_approvals}
    assert mine == {aid: "succeeded" for aid in out.pending_approvals}
    assert len(vendors.writes) == before + len(done)          # one upstream write per executed action
    assert executor.run_approved() == []                       # nothing runs twice
    referents = db.q("SELECT kind FROM session_referents WHERE session_id = %s", (session,))
    assert any(r["kind"] == "list" for r in referents)


def test_epoch_reset_keeps_working(session, brain, vendors, monkeypatch):
    from itsm_agent.config import get_settings
    monkeypatch.setattr(get_settings(), "working_set_budget_tokens", 2500)     # force resets
    out = Orchestrator().ask(session, Q)
    ep = db.q("SELECT seq, close_trigger, handoff FROM epochs WHERE job_id = %s ORDER BY seq", (out.job_id,))
    assert len(ep) > 1 and ep[0]["close_trigger"] == "budget" and ep[0]["handoff"]["handles"]
    assert out.status == "completed" and f"there were {vendors.ds.truth['network_changes']} network" in out.answer_md
