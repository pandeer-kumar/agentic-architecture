"""Design ceiling: ~2,000 unique changes in the window, through the full agent loop."""
import time

from itsm_agent import db
from itsm_agent.connectors import factory
from itsm_agent.harness.orchestrator import Orchestrator

from datetime import datetime, timezone

NOW = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)


def test_two_thousand_records_exact(session, brain):
    fv = factory.fake_vendors(reset=True, now=NOW, n_sn=1850, n_jira=2400, seed=11)
    try:
        t = fv.ds.truth
        assert 1800 <= t["changes_in_window"] <= 2000
        t0 = time.monotonic()
        out = Orchestrator().ask(session, "Which network changes were done in the last 24 hours without CAB approval?")
        elapsed = time.monotonic() - t0
        assert out.status == "completed", out.answer_md
        assert f"there were {t['network_changes']} network changes" in out.answer_md
        assert f"{t['network_without_cab']} of them" in out.answer_md
        biggest = db.q1("SELECT max(token_estimate) AS m FROM context_snapshots WHERE job_id = %s AND agent_role = 'orchestrator'", (out.job_id,))["m"]
        assert biggest < 120_000                       # orchestrator context independent of record count
        print(f"\n2k run: {t['changes_in_window']} changes, {elapsed:.1f}s, max orchestrator payload ~{biggest} tokens")
    finally:
        factory.fake_vendors(reset=True, now=NOW)
