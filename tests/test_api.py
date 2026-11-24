"""HTTP API: ask, fetch job, export list, approve, execute (separation of duties enforced)."""
from fastapi.testclient import TestClient

from itsm_agent.api import app

c = TestClient(app)
H = {"X-User-Subject": "api-user", "X-User-Tz": "Asia/Kolkata"}


def test_api_flow(brain, approver):
    assert c.get("/healthz").json()["ok"]
    sid = c.post("/v1/sessions", headers=H).json()["session_id"]
    r = c.post(f"/v1/sessions/{sid}/messages", headers=H,
               json={"text": "Which network changes were done in the last 24 hours without CAB approval?"}).json()
    assert r["status"] == "completed" and "network changes" in r["answer_md"]
    job = c.get(f"/v1/jobs/{r['job_id']}", headers=H).json()
    assert job["answer"]["status"] == "verified"
    csv = c.get(f"/v1/jobs/{r['job_id']}/lists/L-1.csv", headers=H).text
    assert csv.splitlines()[0].startswith("source_id")
    r2 = c.post(f"/v1/sessions/{sid}/messages", headers=H, json={"text": "Comment on those changes asking for CAB approval"}).json()
    aid = r2["pending_approvals"][0]
    # medium-risk: the requester may approve their own action; a stranger's session is not visible
    assert c.get(f"/v1/jobs/{r['job_id']}", headers={"X-User-Subject": "someone-else"}).status_code == 404
    assert c.post(f"/v1/approvals/{aid}", headers=H, json={"decision": "approve"}).status_code == 200
    assert c.post("/v1/actions/execute", headers=H).status_code == 403            # requester is not an executor
