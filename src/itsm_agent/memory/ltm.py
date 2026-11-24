"""Long-term memory with a promotion gate (docs/03 §4). Retrieval: full text + trigram on subject.
(The vector column is kept for an embedding model you choose; retrieval works without it.)"""
from __future__ import annotations

import psycopg

from .. import db


class MemoryRejected(ValueError):
    pass


def search(query: str, user_id: str, kinds: list[str] | None = None, limit: int = 8) -> list[dict]:
    kinds = kinds or ["semantic", "episodic", "preference"]
    return db.q("""SELECT id, kind, scope, subject_key, content, version, confidence, source_refs, valid_from AS as_of,
                          ts_rank(tsv, websearch_to_tsquery('english', %s)) + similarity(subject_key, %s) AS score
                   FROM memory_items
                   WHERE status = 'active' AND valid_to IS NULL AND (expires_at IS NULL OR expires_at > now())
                     AND kind = ANY(%s) AND (scope = 'org' OR owner_user_id = %s)
                     AND (tsv @@ websearch_to_tsquery('english', %s) OR similarity(subject_key, %s) > 0.3)
                   ORDER BY score DESC, confidence DESC LIMIT %s""",
                (query, query, kinds, user_id, query, query, limit))


def pinned_profile() -> str:
    rows = db.q("""SELECT subject_key, content, version FROM memory_items
                   WHERE pinned AND status = 'active' AND valid_to IS NULL ORDER BY subject_key""")
    return "\n".join(f"- [{r['subject_key']} v{r['version']}] {r['content']}" for r in rows)


def _check_refs(job_id: str, refs: list[str]) -> None:
    if not refs:
        raise MemoryRejected("source_refs are required (provenance)")
    for r in refs:
        if r.startswith("fact:"):
            ok = db.q1("SELECT 1 FROM facts WHERE job_id = %s AND fact_key = %s", (job_id, r[5:]))
        elif r.startswith("rec:"):
            _, system, sid = r.split(":", 2)
            ok = db.q1("""SELECT 1 FROM records rc JOIN result_sets rs ON rs.id = rc.result_set_id
                          WHERE rs.job_id = %s AND rc.source_system = %s AND rc.source_id = %s""", (job_id, system, sid))
        elif r.startswith("user:"):
            ok = True                   # an explicit user statement in this session
        else:
            ok = False
        if not ok:
            raise MemoryRejected(f"source ref {r} does not resolve in this job")


def propose(job_id: str, session_id: str, user_id: str, kind: str, subject_key: str, content: str,
            source_refs: list[str], scope: str) -> dict:
    """Gate: provenance -> no untrusted-text-only facts -> route. Semantic org facts wait for human review."""
    _check_refs(job_id, source_refs)
    if kind == "semantic" and all(r.startswith("rec:") for r in source_refs):
        # a ticket saying something is not enough to make it an org fact (docs/03 §4.2 rule 2)
        status, note = "pending_review", "record-only provenance: needs reviewer confirmation"
    elif kind == "semantic":
        status, note = "pending_review", "org facts are reviewed before use"
    elif kind == "preference":
        status, note = "pending_review", "user must confirm preferences"
    else:
        status, note = "active", "episodic, expires in 180 days"
    owner = user_id if scope == "user" else None
    try:
        row = db.q1("""INSERT INTO memory_items (kind, scope, owner_user_id, subject_key, content, status, confidence, source_refs,
                                                 proposed_by_session, expires_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, CASE WHEN %s = 'episodic' THEN now() + interval '180 days' END)
                       RETURNING id, status""",
                    (kind, scope, owner, subject_key, content[:2000], status, 0.6 if kind == "episodic" else 0.8,
                     db.j(source_refs), session_id, kind))
    except psycopg.errors.UniqueViolation:
        row = db.q1("""INSERT INTO memory_items (kind, scope, owner_user_id, subject_key, content, status, confidence, source_refs,
                                                 proposed_by_session)
                       VALUES (%s,%s,%s,%s,%s,'pending_review',0.5,%s,%s) RETURNING id, status""",
                    (kind, scope, owner, subject_key, content[:2000], db.j(source_refs), session_id))
        note = "conflicts with an active item: sent to review"
    return {"memory_id": str(row["id"]), "status": row["status"], "note": note}


def approve(memory_id: str, reviewer_id: str, pin: bool = False) -> dict:
    with db.tx() as c:
        item = c.execute("SELECT * FROM memory_items WHERE id = %s FOR UPDATE", (memory_id,)).fetchone()
        if item["supersedes_id"]:
            c.execute("UPDATE memory_items SET valid_to = now(), status = 'deprecated' WHERE id = %s", (item["supersedes_id"],))
        c.execute("""UPDATE memory_items SET valid_to = now(), status = 'deprecated'
                     WHERE status = 'active' AND valid_to IS NULL AND subject_key = %s AND scope = %s AND id <> %s
                       AND coalesce(owner_user_id::text,'') = coalesce(%s::text,'')""",
                  (item["subject_key"], item["scope"], memory_id, item["owner_user_id"]))
        return c.execute("""UPDATE memory_items SET status = 'active', reviewed_by = %s, reviewed_at = now(), pinned = %s
                            WHERE id = %s RETURNING id, status, pinned""", (reviewer_id, pin and item["kind"] == "semantic", memory_id)).fetchone()


def flag_conflict(memory_id: str, note: str) -> dict:
    row = db.q1("""UPDATE memory_items SET confidence = greatest(0, confidence - 0.2),
                     status = CASE WHEN confidence - 0.2 < 0.3 THEN 'pending_review' ELSE status END
                   WHERE id = %s RETURNING id, status, confidence""", (memory_id,))
    if not row:
        raise MemoryRejected("unknown memory id")
    return {"memory_id": str(row["id"]), "status": row["status"], "confidence": float(row["confidence"]), "note": note[:200]}
