"""Crash in the middle of a job, then resume from durable state: same exact answer, nothing lost."""
import pytest

from itsm_agent import db
from itsm_agent.harness import orchestrator as orch_mod
from itsm_agent.harness.orchestrator import Orchestrator
from itsm_agent.tools import gateway

Q = "Which network changes were done in the last 24 hours without CAB approval?"


class Crash(Exception):
    pass


def test_crash_mid_job_then_resume(session, brain, vendors, monkeypatch):
    real = gateway.call
    calls = {"n": 0}

    def crashing(ctx, name, args):
        calls["n"] += 1
        if name == "analyzer_classify":                 # die right after a costly step starts
            raise Crash()
        return real(ctx, name, args)

    monkeypatch.setattr(orch_mod.tool_gateway, "call", crashing)
    with pytest.raises(Crash):
        Orchestrator().ask(session, Q)
    job = db.q1("SELECT id, status FROM jobs WHERE session_id = %s ORDER BY created_at DESC LIMIT 1", (session,))
    assert job["status"] == "running"
    fetched_before = db.q1("SELECT count(*) AS n FROM result_sets WHERE job_id = %s AND kind = 'fetch'", (job["id"],))["n"]

    monkeypatch.setattr(orch_mod.tool_gateway, "call", real)                 # "restart the worker"
    out = Orchestrator().resume(str(job["id"]))
    t = vendors.ds.truth
    assert out.status == "completed"
    assert f"there were {t['network_changes']} network changes" in out.answer_md
    fetched_after = db.q1("SELECT count(*) AS n FROM result_sets WHERE job_id = %s AND kind = 'fetch'", (job["id"],))["n"]
    assert fetched_after == fetched_before == 2                                   # completed fetches were not redone
