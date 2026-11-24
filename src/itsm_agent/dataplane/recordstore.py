"""Record store: result sets, paged writes with checkpoints, completeness proof, handle summaries."""
from __future__ import annotations

import secrets
from collections import Counter
from datetime import datetime
from typing import Iterable

from .. import db
from ..connectors.base import CanonicalRecord, FetchSummary, OverLimit, Page, Window

SUMMARY_FIELDS = ["source_system", "status", "category", "cab_status", "project", "assignment_group", "components", "tags"]


def new_handle() -> str:
    return "rs_" + secrets.token_hex(5).upper()


def create_fetch_set(job_id: str, query_spec_id: str, system: str, max_records: int) -> dict:
    return db.q1("""INSERT INTO result_sets (handle, job_id, query_spec_id, kind, source_system, max_records)
                    VALUES (%s,%s,%s,'fetch',%s,%s) RETURNING *""",
                 (new_handle(), job_id, query_spec_id, system, max_records))


def write_page(rs: dict, page: Page) -> None:
    with db.tx() as c:
        c.execute("""INSERT INTO result_set_pages (result_set_id, page_no, cursor_in, cursor_out, row_count)
                     VALUES (%s,%s,%s,%s,%s)""", (rs["id"], page.page_no, page.cursor_in, page.cursor_out, len(page.records)))
        for r in page.records:
            row = c.execute("""
                INSERT INTO records (result_set_id, source_system, source_id, source_url, title, body_text, status,
                                     created_at_src, updated_at_src, event_at, fields, content_hash)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (result_set_id, source_system, source_id) DO UPDATE
                  SET title = EXCLUDED.title, body_text = EXCLUDED.body_text, status = EXCLUDED.status,
                      updated_at_src = EXCLUDED.updated_at_src, event_at = EXCLUDED.event_at,
                      fields = EXCLUDED.fields, content_hash = EXCLUDED.content_hash
                  WHERE records.updated_at_src IS NULL OR EXCLUDED.updated_at_src >= records.updated_at_src
                RETURNING id""",
                (rs["id"], r.source_system, r.source_id, r.source_url, r.title, r.body_text, r.status, r.created_at,
                 r.updated_at, r.event_at, db.j(r.fields), r.content_hash)).fetchone()
            if row:
                c.execute("INSERT INTO result_set_members VALUES (%s,%s) ON CONFLICT DO NOTHING", (rs["id"], row["id"]))


def finalize(rs: dict, summary: FetchSummary, window: Window) -> dict:
    """Completeness proof (docs/04 §3): C1 exhaustion, C2 count match, C3 bound, C4 duplicates, C5 window."""
    with db.tx() as c:
        # C5: drop rows whose window field falls outside [start, end) after UTC normalisation
        out = c.execute("""DELETE FROM result_set_members m USING records r
                           WHERE m.record_id = r.id AND m.result_set_id = %s
                             AND (r.event_at IS NULL OR r.event_at < %s OR r.event_at >= %s) RETURNING r.id""",
                        (rs["id"], window.start, window.end)).fetchall()
        unique = c.execute("SELECT count(*) AS n FROM result_set_members WHERE result_set_id = %s", (rs["id"],)).fetchone()["n"]
        stored = c.execute("SELECT count(*) AS n FROM records WHERE result_set_id = %s", (rs["id"],)).fetchone()["n"]
        notes = list(summary.notes)
        status = "incomplete"
        method = None
        if summary.exhausted:
            if summary.source_reported_total is not None:
                if summary.source_reported_total == stored:
                    status, method = "proven", "count_match"
                else:
                    notes.append(f"source reported {summary.source_reported_total}, stored {stored}")
            elif summary.approximate_count is not None:
                tol = max(2, round(0.01 * summary.approximate_count))
                status = "proven" if summary.approximate_count == stored else ("bounded" if abs(summary.approximate_count - stored) <= tol else "incomplete")
                method = "page_exhaustion"
            else:
                status, method = "bounded", "page_exhaustion"
        if out:
            notes.append(f"{len(out)} rows outside the window excluded after UTC check")
        proof = {"status": status, "method": method, "pages": summary.pages, "fetched_rows": summary.fetched_rows,
                 "stored_unique": stored, "in_window": unique, "duplicates_resolved": summary.fetched_rows - stored,
                 "source_reported_total": summary.source_reported_total, "approximate_count": summary.approximate_count,
                 "notes": notes}
        # result_sets CHECK: 'proven' requires source_reported_total == unique_rows, so store the
        # source total only when it describes the same population (no out-of-window exclusions)
        total_col = summary.source_reported_total if (status == "proven" and stored == unique) else None
        row = c.execute("""UPDATE result_sets SET status = 'complete', completeness_status = %s, completeness = %s,
                             source_reported_total = %s, approximate_count = %s, fetched_rows = %s, unique_rows = %s,
                             completed_at = now() WHERE id = %s RETURNING *""",
                        (status, db.j(proof), total_col, summary.approximate_count, max(summary.fetched_rows, unique),
                         unique, rs["id"])).fetchone()
    return row


def mark_over_limit(rs: dict, e: OverLimit) -> dict:
    return db.q1("""UPDATE result_sets SET status = 'over_limit', completeness = %s WHERE id = %s RETURNING *""",
                 (db.j({"status": "over_limit", "count": e.count, "limit": e.limit}), rs["id"]))


def get_by_handle(job_id: str, handle: str) -> dict:
    rs = db.q1("SELECT * FROM result_sets WHERE job_id = %s AND handle = %s", (job_id, handle))
    if not rs:
        raise KeyError(f"unknown handle {handle} for this job")
    return rs


def handle_summary(rs: dict, sample_n: int = 5) -> dict:
    """What the model sees instead of the rows (docs/06 §3)."""
    stats = {}
    for f in SUMMARY_FIELDS:
        if f in ("source_system", "status"):
            rows = db.q(f"""SELECT r.{f} AS v, count(*) AS n FROM result_set_members m JOIN records r ON r.id = m.record_id
                            WHERE m.result_set_id = %s GROUP BY 1 ORDER BY 2 DESC LIMIT 8""", (rs["id"],))
        elif f in ("components", "tags"):
            rows = db.q("""SELECT x AS v, count(*) AS n FROM result_set_members m JOIN records r ON r.id = m.record_id,
                             jsonb_array_elements_text(coalesce(r.fields->%s, '[]'::jsonb)) x
                           WHERE m.result_set_id = %s GROUP BY 1 ORDER BY 2 DESC LIMIT 8""", (f, rs["id"]))
        else:
            rows = db.q("""SELECT r.fields->>%s AS v, count(*) AS n FROM result_set_members m JOIN records r ON r.id = m.record_id
                           WHERE m.result_set_id = %s GROUP BY 1 ORDER BY 2 DESC LIMIT 8""", (f, rs["id"]))
        vals = {str(r["v"]): r["n"] for r in rows if r["v"] is not None}
        if vals:
            stats[f] = vals
    sample = db.q("""SELECT r.source_system, r.source_id, r.title, r.status FROM result_set_members m
                     JOIN records r ON r.id = m.record_id WHERE m.result_set_id = %s ORDER BY r.id LIMIT %s""", (rs["id"], sample_n))
    spec = db.q1("SELECT window_start, window_end, window_field FROM query_specs WHERE id = %s", (rs["query_spec_id"],)) if rs.get("query_spec_id") else None
    return {"handle": rs["handle"], "source": rs["source_system"], "kind": rs["kind"], "row_count": rs["unique_rows"],
            "status": rs["status"], "completeness": rs["completeness"],
            "window_utc": [spec["window_start"].isoformat(), spec["window_end"].isoformat()] if spec else None,
            "window_field": spec["window_field"] if spec else None, "field_stats": stats,
            "sample": [f"{s['source_system']}:{s['source_id']} | {s['title'][:80]} | {s['status']}" for s in sample],
            "note": "Sample is for orientation only. Never count from it; use analyzer_aggregate."}
