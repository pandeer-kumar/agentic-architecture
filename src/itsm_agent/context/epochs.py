"""Epochs (docs/01 §4): one append-only conversation each; closed with a structured, validated handoff."""
from __future__ import annotations

from .. import db
from ..memory import stm


def open_epoch(job_id: str, model: str, tool_names: list[str]) -> dict:
    seq = db.q1("SELECT coalesce(max(seq), 0) + 1 AS n FROM epochs WHERE job_id = %s AND agent_role = 'orchestrator'", (job_id,))["n"]
    return db.q1("""INSERT INTO epochs (job_id, seq, model, tool_names) VALUES (%s,%s,%s,%s) RETURNING *""",
                 (job_id, seq, model, sorted(tool_names)))


def current_epoch(job_id: str) -> dict | None:
    return db.q1("SELECT * FROM epochs WHERE job_id = %s AND agent_role = 'orchestrator' AND closed_at IS NULL", (job_id,))


def build_handoff(job_id: str, last_user_turn: str) -> dict:
    """Deterministic handoff from durable state: ids and numbers are copied from the DB, never from prose."""
    job = db.q1("SELECT ledger FROM jobs WHERE id = %s", (job_id,))
    led = job["ledger"]
    sp = stm.active_scratchpad(job_id)
    return {"goal": led.get("goal") or last_user_turn, "window_utc": led.get("window_utc"), "user_tz": led.get("user_tz"),
            "query_specs": led.get("query_specs", []), "handles": led.get("handles", []), "facts": led.get("facts", []),
            "lists": led.get("lists", []), "steps": led.get("steps", {}),
            "decisions": [f"sp:{e['id']} {e['text']}" for e in sp if e["entry_type"] == "decision"],
            "open_questions": [e["text"] for e in sp if e["entry_type"] == "open_question"],
            "last_user_turn": last_user_turn}


def validate_handoff(job_id: str, handoff: dict) -> list[str]:
    """Every complete result set and every fact of the job must survive into the next epoch."""
    problems = []
    handles = {r["handle"] for r in db.q("SELECT handle FROM result_sets WHERE job_id = %s AND status = 'complete'", (job_id,))}
    missing = handles - {h["handle"] for h in handoff["handles"]}
    if missing:
        problems.append(f"handles missing from handoff: {sorted(missing)}")
    facts = {r["fact_key"] for r in db.q("SELECT fact_key FROM facts WHERE job_id = %s", (job_id,))}
    if facts - {f["key"] for f in handoff["facts"]}:
        problems.append(f"facts missing from handoff: {sorted(facts - {f['key'] for f in handoff['facts']})}")
    return problems


def close_epoch(epoch_id: str, trigger: str, handoff: dict | None, turns: int, usage: dict) -> None:
    db.q("""UPDATE epochs SET closed_at = now(), close_trigger = %s, handoff = %s, turns = %s,
              input_tokens = %s, output_tokens = %s, cache_read_tokens = %s WHERE id = %s""",
         (trigger, db.j(handoff) if handoff is not None else None, min(turns, 40), usage.get("input_tokens", 0),
          usage.get("output_tokens", 0), usage.get("cache_read_input_tokens", 0), epoch_id))
