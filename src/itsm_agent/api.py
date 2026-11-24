"""HTTP API (FastAPI). Development auth: X-User-Subject header (replace with your IdP's OIDC middleware)."""
from __future__ import annotations

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from . import db, seed
from .context import snapshots
from .dataplane import analyzer
from .harness.orchestrator import Orchestrator, new_session
from .memory import ltm
from .tools import executor

app = FastAPI(title="ITSM Agent", version="0.1.0")


def current_user(x_user_subject: str = Header(...), x_user_tz: str = Header("UTC")) -> dict:
    u = db.q1("SELECT * FROM users WHERE external_subject = %s", (x_user_subject,))
    if not u:
        uid = seed.ensure_user(x_user_subject, f"{x_user_subject}@localhost", x_user_subject, x_user_tz)
        u = db.q1("SELECT * FROM users WHERE id = %s", (uid,))
    u["roles"] = [r["role"] for r in db.q("SELECT role FROM user_roles WHERE user_id = %s", (u["id"],))]
    return u


class Ask(BaseModel):
    text: str


class Decision(BaseModel):
    decision: str
    comment: str | None = None


@app.get("/healthz")
def health():
    db.q("SELECT 1")
    return {"ok": True}


@app.post("/v1/sessions")
def create_session(user=Depends(current_user)):
    return {"session_id": new_session(str(user["id"]))}


@app.post("/v1/sessions/{session_id}/messages")
def ask(session_id: str, body: Ask, user=Depends(current_user)):
    s = db.q1("SELECT user_id FROM sessions WHERE id = %s", (session_id,))
    if not s or s["user_id"] != user["id"]:
        raise HTTPException(404, "session not found")
    out = Orchestrator().ask(session_id, body.text)
    return {"job_id": out.job_id, "status": out.status, "banner": out.banner, "answer_md": out.answer_md,
            "pending_approvals": out.pending_approvals}


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: str, user=Depends(current_user)):
    j = db.q1("SELECT * FROM jobs WHERE id = %s AND user_id = %s", (job_id, user["id"]))
    if not j:
        raise HTTPException(404)
    a = db.q1("SELECT id, status, banner, rendered_md FROM answers WHERE job_id = %s ORDER BY revision DESC LIMIT 1", (job_id,))
    return {"job": {k: j[k] for k in ("id", "status", "banner", "ledger", "created_at", "completed_at")}, "answer": a}


@app.get("/v1/jobs/{job_id}/lists/{list_key}.csv", response_class=PlainTextResponse)
def export_list(job_id: str, list_key: str, user=Depends(current_user)):
    if not db.q1("SELECT 1 FROM jobs WHERE id = %s AND user_id = %s", (job_id, user["id"])):
        raise HTTPException(404)
    return analyzer.export_csv(job_id, list_key)


@app.get("/v1/approvals")
def pending(user=Depends(current_user)):
    return db.q("SELECT action_id, tool_name, risk_tier, required_approvals, approvals, preview, expires_at FROM v_pending_approvals")


@app.post("/v1/approvals/{action_id}")
def decide(action_id: str, body: Decision, user=Depends(current_user)):
    if body.decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve or reject")
    try:
        return executor.decide(action_id, str(user["id"]), body.decision, body.comment)
    except Exception as e:                                  # DB triggers enforce SoD/roles/expiry
        raise HTTPException(409, str(e).split("\n")[0])


@app.post("/v1/actions/execute")
def execute(user=Depends(current_user)):
    if "admin" not in user["roles"] and "approver" not in user["roles"]:
        raise HTTPException(403)
    return executor.run_approved()


@app.get("/v1/snapshots/{snapshot_id}/request")
def replay(snapshot_id: str, user=Depends(current_user)):
    if "admin" not in user["roles"]:
        raise HTTPException(403)
    return snapshots.replay_request(snapshot_id)


@app.post("/v1/memory/{memory_id}/approve")
def approve_memory(memory_id: str, pin: bool = False, user=Depends(current_user)):
    if "admin" not in user["roles"]:
        raise HTTPException(403)
    return ltm.approve(memory_id, str(user["id"]), pin)
