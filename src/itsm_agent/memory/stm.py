"""Short-term memory: session state, referents, scratchpad (docs/03 §2-3). Invariants are DB-enforced."""
from __future__ import annotations

import psycopg

from .. import db


class ScratchpadRejected(ValueError):
    pass


def ensure_session_state(session_id: str, tz: str) -> dict:
    db.q("""INSERT INTO session_state (session_id, user_tz) VALUES (%s,%s) ON CONFLICT (session_id) DO NOTHING""", (session_id, tz))
    return db.q1("SELECT * FROM session_state WHERE session_id = %s", (session_id,))


def update_summary(session_id: str, question: str, headline: str) -> None:
    st = db.q1("SELECT summary FROM session_state WHERE session_id = %s", (session_id,))
    lines = [l for l in (st["summary"] if st else "").split("\n") if l.strip()]
    lines.append(f"Q: {question[:300]} -> A: {headline[:300]}")
    text = "\n".join(lines)
    while len(text) > 3800 and len(lines) > 1:          # rolling: oldest turns drop first
        lines.pop(0)
        text = "\n".join(lines)
    db.q("""UPDATE session_state SET summary = %s, active_goal = %s, version = version + 1, updated_at = now()
            WHERE session_id = %s""", (text, question[:300], session_id))


def add_referent(session_id: str, job_id: str, label: str, kind: str, target: str, turn: int,
                 ordinal_map: dict | None = None) -> str:
    with db.tx() as c:
        c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"ref:{session_id}",))
        n = c.execute("SELECT count(*) AS n FROM session_referents WHERE session_id = %s", (session_id,)).fetchone()["n"] + 1
        c.execute("""INSERT INTO session_referents (session_id, referent_key, label, kind, target, job_id, ordinal_map, turn)
                     VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                  (session_id, f"ref-{n}", label[:200], kind, target, job_id, db.j(ordinal_map or {}), turn))
    return f"ref-{n}"


def recent_referents(session_id: str, n: int = 10) -> list[dict]:
    return db.q("""SELECT referent_key, label, kind, target, job_id, ordinal_map FROM session_referents
                   WHERE session_id = %s ORDER BY id DESC LIMIT %s""", (session_id, n))


def resolve_referent(session_id: str, referent_key: str) -> dict:
    r = db.q1("SELECT * FROM session_referents WHERE session_id = %s AND referent_key = %s", (session_id, referent_key))
    if not r:
        raise KeyError(f"unknown referent {referent_key} in this session")
    return {"referent_key": r["referent_key"], "label": r["label"], "kind": r["kind"], "target": r["target"],
            "from_job": str(r["job_id"]), "ordinal_map": r["ordinal_map"]}


def scratchpad_write(job_id: str, entry_type: str, text: str, refs: list[str], supersedes_id: int | None,
                     owner: str = "orchestrator") -> dict:
    try:
        row = db.q1("""INSERT INTO scratchpad_entries (job_id, owner, entry_type, text, refs, supersedes_id)
                       VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""", (job_id, owner, entry_type, text, refs, supersedes_id))
    except psycopg.errors.CheckViolation as e:
        raise ScratchpadRejected(str(e).split("\n")[0]) from e
    except psycopg.errors.UniqueViolation as e:
        raise ScratchpadRejected("that entry was already superseded") from e
    return {"id": row["id"], "entry_type": entry_type}


def active_scratchpad(job_id: str, owner: str = "orchestrator") -> list[dict]:
    return db.q("""SELECT id, entry_type, text, refs FROM v_scratchpad_active WHERE job_id = %s AND owner = %s
                   ORDER BY (entry_type = 'warning') DESC, id DESC""", (job_id, owner))


def compress_scratchpad(job_id: str, owner: str = "orchestrator", keep: int = 30) -> int:
    """Merge the oldest todo/hypothesis entries into one (refs preserved). Returns entries merged."""
    rows = db.q("""SELECT id, entry_type, text, refs FROM v_scratchpad_active WHERE job_id = %s AND owner = %s
                   AND entry_type IN ('todo','hypothesis') ORDER BY id""", (job_id, owner))
    active = len(active_scratchpad(job_id, owner))
    if active <= keep or len(rows) < 2:
        return 0
    take = rows[: max(2, active - keep + 1)]
    text = ("merged: " + " | ".join(r["text"][:40] for r in take))[:300]
    refs = sorted({x for r in take for x in r["refs"]})
    db.q("""INSERT INTO scratchpad_entries (job_id, owner, entry_type, text, refs, merged_from) VALUES (%s,%s,'todo',%s,%s,%s)""",
         (job_id, owner, text, refs, [r["id"] for r in take]))
    return len(take)
