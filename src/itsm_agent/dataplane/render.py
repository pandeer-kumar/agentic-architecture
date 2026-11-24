"""Renderer (docs/04 §8): placeholders are expanded from the database, so lists and numbers are exact."""
from __future__ import annotations

import re
from zoneinfo import ZoneInfo

from .. import db
from . import analyzer

PH = re.compile(r"\{\{\s*(list:)?(F-\d+|L-\d+)(\.total|\.value)?\s*\}\}")
MAX_ROWS = 25


def fact_scalar(value: dict):
    return value.get("value", value.get("total"))


def expand(job_id: str, narrative: str) -> str:
    def sub(m):
        is_list, key = m.group(1), m.group(2)
        if is_list or key.startswith("L-"):
            cols, rows, total = analyzer.list_rows(job_id, key, MAX_ROWS)
            head = "| " + " | ".join(cols) + " |\n|" + "---|" * len(cols)
            body = "\n".join("| " + " | ".join(_cell(r, c, i == 0) for i, c in enumerate(cols)) + " |" for r in rows)
            more = f"\n\n_{total - len(rows)} more rows: export `/v1/jobs/{job_id}/lists/{key}.csv`_" if total > len(rows) else ""
            return f"\n\n{head}\n{body}{more}\n"
        f = db.q1("SELECT value FROM facts WHERE job_id = %s AND fact_key = %s", (job_id, key))
        if not f:
            return m.group(0)
        return str(fact_scalar(f["value"]))
    return PH.sub(sub, narrative)


def _cell(r: dict, c: str, link: bool) -> str:
    v = r.get(c)
    s = ", ".join(map(str, v)) if isinstance(v, list) else ("" if v is None else str(v))
    s = s.replace("|", "\\|")[:120]
    return f"[{s}]({r['_url']})" if link else s


def footer(job_id: str, banner: str) -> str:
    job = db.q1("SELECT ledger FROM jobs WHERE id = %s", (job_id,))
    spec = db.q1("SELECT * FROM query_specs WHERE job_id = %s ORDER BY frozen_at LIMIT 1", (job_id,))
    lines = []
    label = {"verified": "✅ Verified", "verified_with_caveats": "✅ Verified with caveats", "partially_verified": "⚠️ Partially verified"}[banner]
    lines.append(f"**{label}**")
    if spec:
        z = ZoneInfo(spec["user_tz"])
        lines.append(f"Window ({spec['window_field']}): {spec['window_start'].astimezone(z):%Y-%m-%d %H:%M} → "
                     f"{spec['window_end'].astimezone(z):%Y-%m-%d %H:%M} {spec['user_tz']} "
                     f"({spec['window_start']:%Y-%m-%d %H:%M} → {spec['window_end']:%Y-%m-%d %H:%M} UTC), as of {spec['frozen_at']:%Y-%m-%d %H:%M} UTC")
    for rs in db.q("SELECT handle, source_system, unique_rows, completeness_status, completeness FROM result_sets WHERE job_id = %s AND kind = 'fetch' ORDER BY created_at", (job_id,)):
        lines.append(f"Source {rs['source_system']}: {rs['unique_rows']} records, completeness **{rs['completeness_status']}**"
                     + (f" ({rs['completeness'].get('method')})" if rs["completeness"].get("method") else ""))
    nr = db.q1("SELECT count(*) AS n FROM record_annotations WHERE job_id = %s AND is_final AND needs_human_review", (job_id,))
    if nr and nr["n"]:
        lines.append(f"{nr['n']} records could not be classified reliably and need human review.")
    warnings = db.q("SELECT text FROM v_scratchpad_active WHERE job_id = %s AND entry_type = 'warning'", (job_id,))
    lines += [f"Caveat: {w['text']}" for w in warnings]
    return "\n\n---\n" + "  \n".join(lines)
