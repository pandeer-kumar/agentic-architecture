"""Analyzer (custom MCP server #6): deterministic computation over the record store.

Numbers in answers come only from analyzer_aggregate facts (stored SQL + params + inputs digest,
immutable, reproducible). Filters are structured (field / op / value) and compiled to SQL here;
the model never writes SQL against the record store.
"""
from __future__ import annotations

import csv
import hashlib
import io
from typing import Any

from psycopg import sql

from .. import db
from . import recordstore

COLUMNS = {"source_system", "source_id", "title", "status", "event_at", "source_url"}
OPS = {"eq", "neq", "in", "not_in", "contains", "gte", "lt", "is_null", "not_null"}


class AnalyzerError(ValueError):
    pass


def _field_expr(field: str) -> sql.Composable:
    if field in COLUMNS:
        return sql.SQL("r.{}").format(sql.Identifier(field))
    if not field.replace("_", "").isalnum():
        raise AnalyzerError(f"invalid field name {field!r}")
    return sql.SQL("(r.fields->>{})").format(sql.Literal(field))


def _where(filters: list[dict], label_filter: str | None, job_id: str) -> tuple[sql.Composable, list]:
    parts: list[sql.Composable] = []
    params: list[Any] = []
    for f in filters or []:
        op, field, value = f.get("op"), f.get("field"), f.get("value")
        if op not in OPS:
            raise AnalyzerError(f"unknown op {op!r}; use one of {sorted(OPS)}")
        e = _field_expr(field)
        if field in ("components", "tags"):            # array fields
            arr = sql.SQL("coalesce(r.fields->{}, '[]'::jsonb)").format(sql.Literal(field))
            if op == "contains" or op == "eq":
                parts.append(sql.SQL("{} ? %s").format(arr)); params.append(value)
            elif op == "neq":
                parts.append(sql.SQL("NOT ({} ? %s)").format(arr)); params.append(value)
            else:
                raise AnalyzerError(f"op {op} not supported on array field {field}")
            continue
        if op == "eq":
            parts.append(sql.SQL("{} = %s").format(e)); params.append(value)
        elif op == "neq":
            parts.append(sql.SQL("{} IS DISTINCT FROM %s").format(e)); params.append(value)
        elif op in ("in", "not_in"):
            vals = [v.strip() for v in str(value).split(",")]
            tmpl = "{} = ANY(%s)" if op == "in" else "NOT ({} = ANY(%s))"
            parts.append(sql.SQL(tmpl).format(e))
            params.append(vals)
        elif op == "contains":
            parts.append(sql.SQL("{} ILIKE %s").format(e)); params.append(f"%{value}%")
        elif op == "gte":
            parts.append(sql.SQL("{} >= %s").format(e)); params.append(value)
        elif op == "lt":
            parts.append(sql.SQL("{} < %s").format(e)); params.append(value)
        elif op == "is_null":
            parts.append(sql.SQL("{} IS NULL").format(e))
        elif op == "not_null":
            parts.append(sql.SQL("{} IS NOT NULL").format(e))
    if label_filter:
        if ":" not in label_filter:
            raise AnalyzerError("label_filter must be 'rubric_id:label'")
        rubric, label = label_filter.split(":", 1)
        parts.append(sql.SQL("""EXISTS (SELECT 1 FROM record_annotations a WHERE a.record_id = r.id AND a.job_id = %s
                                 AND a.rubric_id = %s AND a.is_final AND a.label = %s)"""))
        params += [job_id, rubric, label]
    if not parts:
        return sql.SQL("TRUE"), params
    return sql.SQL(" AND ").join(parts), params


def _digest(result_set_ids: list[str]) -> str:
    rows = db.q("""SELECT r.content_hash FROM result_set_members m JOIN records r ON r.id = m.record_id
                   WHERE m.result_set_id = ANY(%s::uuid[]) ORDER BY r.id""", (result_set_ids,))
    return hashlib.sha256("".join(r["content_hash"] for r in rows).encode()).hexdigest()


def describe(job_id: str, handle: str) -> dict:
    return recordstore.handle_summary(recordstore.get_by_handle(job_id, handle))


def sample(job_id: str, handle: str, n: int = 10, filters: list[dict] | None = None, label_filter: str | None = None) -> list[dict]:
    rs = recordstore.get_by_handle(job_id, handle)
    where, params = _where(filters or [], label_filter, job_id)
    q = sql.SQL("""SELECT r.source_system, r.source_id, r.source_url, r.title, r.body_text, r.status, r.fields, r.content_hash
                   FROM result_set_members m JOIN records r ON r.id = m.record_id
                   WHERE m.result_set_id = %s AND {} ORDER BY r.id LIMIT %s""").format(where)
    with db.tx() as c:
        return c.execute(q, [rs["id"], *params, min(int(n), 25)]).fetchall()


def _derive(job_id: str, parents: list[dict], derivation: dict, record_ids_sql: sql.Composable, params: list) -> dict:
    with db.tx() as c:
        rs = c.execute("""INSERT INTO result_sets (handle, job_id, kind, source_system, derived_from, derivation, status,
                                                   completeness_status, completeness)
                          VALUES (%s,%s,'derived','analyzer',%s,%s,'complete',%s,%s) RETURNING *""",
                       (recordstore.new_handle(), job_id, [p["id"] for p in parents], db.j(derivation),
                        _weakest([p["completeness_status"] for p in parents]),
                        db.j({"status": _weakest([p["completeness_status"] for p in parents]), "method": "derived",
                              "parents": {p["handle"]: p["completeness_status"] for p in parents}}))).fetchone()
        c.execute(sql.SQL("INSERT INTO result_set_members (result_set_id, record_id) SELECT %s, id FROM ({}) x").format(record_ids_sql),
                  [rs["id"], *params])
        n = c.execute("SELECT count(*) AS n FROM result_set_members WHERE result_set_id = %s", (rs["id"],)).fetchone()["n"]
        return c.execute("UPDATE result_sets SET unique_rows = %s, fetched_rows = %s WHERE id = %s RETURNING *",
                         (n, n, rs["id"])).fetchone()


def _weakest(statuses: list[str]) -> str:
    order = ["incomplete", "unknown", "bounded", "proven"]
    return min(statuses, key=order.index) if statuses else "unknown"


def filter_(job_id: str, handle: str, filters: list[dict], label_filter: str | None = None) -> dict:
    rs = recordstore.get_by_handle(job_id, handle)
    where, params = _where(filters, label_filter, job_id)
    q = sql.SQL("SELECT r.id FROM result_set_members m JOIN records r ON r.id = m.record_id WHERE m.result_set_id = %s AND {}").format(where)
    out = _derive(job_id, [rs], {"op": "filter", "from": handle, "filters": filters, "label_filter": label_filter}, q, [rs["id"], *params])
    return recordstore.handle_summary(out)


def union(job_id: str, handles: list[str], dedupe_key: str) -> dict:
    """Union in priority order; for equal dedupe keys the record from the earlier handle wins."""
    parents = [recordstore.get_by_handle(job_id, h) for h in handles]
    key = _field_expr(dedupe_key)
    q = sql.SQL("""SELECT id FROM (
                     SELECT r.id, row_number() OVER (PARTITION BY coalesce({key}, r.source_system || ':' || r.source_id)
                                                     ORDER BY array_position(%s::uuid[], m.result_set_id), r.id) AS rn
                     FROM result_set_members m JOIN records r ON r.id = m.record_id
                     WHERE m.result_set_id = ANY(%s::uuid[])) t WHERE rn = 1""").format(key=key)
    ids = [p["id"] for p in parents]
    out = _derive(job_id, parents, {"op": "union", "handles": handles, "dedupe_key": dedupe_key}, q, [ids, ids])
    total = sum(p["unique_rows"] for p in parents)
    summary = recordstore.handle_summary(out)
    summary["duplicates_removed"] = total - out["unique_rows"]
    return summary


def join(job_id: str, left: str, right: str, left_key: str, right_key: str) -> dict:
    L, R = recordstore.get_by_handle(job_id, left), recordstore.get_by_handle(job_id, right)
    lk, rk = _field_expr(left_key), _field_expr(right_key)
    def side(a, b, ak, bk, matched):
        return sql.SQL("""SELECT r.id FROM result_set_members m JOIN records r ON r.id = m.record_id
                          WHERE m.result_set_id = %s AND {neg} EXISTS (
                            SELECT 1 FROM result_set_members m2 JOIN records r2 ON r2.id = m2.record_id
                            WHERE m2.result_set_id = %s AND {bk2} = {ak})""").format(
            neg=sql.SQL("") if matched else sql.SQL("NOT"), ak=ak,
            bk2=sql.SQL("(r2.fields->>{})").format(sql.Literal(b)) if b not in COLUMNS else sql.SQL("r2.{}").format(sql.Identifier(b)))
    matched = _derive(job_id, [L, R], {"op": "join_matched", "left": left, "right": right}, side(L, right_key, lk, right_key, True), [L["id"], R["id"]])
    left_only = _derive(job_id, [L, R], {"op": "join_left_only", "left": left, "right": right}, side(L, right_key, lk, right_key, False), [L["id"], R["id"]])
    right_only = _derive(job_id, [R, L], {"op": "join_right_only", "left": left, "right": right}, side(R, left_key, rk, left_key, False), [R["id"], L["id"]])
    return {"matched_left": recordstore.handle_summary(matched)["handle"], "matched_count": matched["unique_rows"],
            "left_only": left_only["handle"], "left_only_count": left_only["unique_rows"],
            "right_only": right_only["handle"], "right_only_count": right_only["unique_rows"]}


def aggregate(job_id: str, handle: str, metric: str = "count", filters: list[dict] | None = None,
              group_by: list[str] | None = None, metric_field: str | None = None, label_filter: str | None = None,
              description: str | None = None) -> dict:
    rs = recordstore.get_by_handle(job_id, handle)
    where, params = _where(filters or [], label_filter, job_id)
    if metric == "count":
        m = sql.SQL("count(*)")
    elif metric == "count_distinct":
        m = sql.SQL("count(DISTINCT {})").format(_field_expr(metric_field or "source_id"))
    elif metric in ("sum", "min", "max"):
        m = sql.SQL("{}(({})::numeric)").format(sql.SQL(metric), _field_expr(metric_field or ""))
    else:
        raise AnalyzerError(f"unknown metric {metric}")
    gb = [_field_expr(g) for g in (group_by or [])]
    select = sql.SQL(", ").join([*(sql.SQL("{} AS {}").format(e, sql.Identifier(f"g{i}")) for i, e in enumerate(gb)),
                                 sql.SQL("{} AS value").format(m)])
    q = sql.SQL("SELECT {sel} FROM result_set_members m JOIN records r ON r.id = m.record_id "
                "WHERE m.result_set_id = %s AND {w}{gb}").format(
        sel=select, w=where,
        gb=sql.SQL(" GROUP BY {} ORDER BY value DESC").format(sql.SQL(", ").join(sql.SQL(f"g{i}") for i in range(len(gb)))) if gb else sql.SQL(""))
    qparams = [rs["id"], *params]
    with db.tx() as c:
        sql_text = q.as_string(c)
        rows = c.execute(q, qparams).fetchall()
        if gb:
            groups = [{"key": [r[f"g{i}"] for i in range(len(gb))], "value": _num(r["value"])} for r in rows]
            value = {"groups": groups, "total": sum(g["value"] or 0 for g in groups)}
        else:
            value = {"value": _num(rows[0]["value"]) if rows else 0}
        c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"facts:{job_id}",))
        n = c.execute("SELECT count(*) AS n FROM facts WHERE job_id = %s", (job_id,)).fetchone()["n"] + 1
        fact = c.execute("""INSERT INTO facts (job_id, fact_key, description, sql_text, params, input_result_set_ids, input_digest, value)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                         (job_id, f"F-{n}", description or f"{metric} over {handle}", sql_text,
                          db.j([str(p) if not isinstance(p, (list, int, float)) else p for p in qparams]),
                          [rs["id"]], _digest([str(rs["id"])]), db.j(value))).fetchone()
    return {"fact_key": fact["fact_key"], "value": value, "description": fact["description"],
            "completeness": rs["completeness_status"]}


def recompute_fact(fact: dict) -> Any:
    """D5: re-run a fact's SQL against the stored inputs; must equal the stored value."""
    with db.tx() as c:
        rows = c.execute(fact["sql_text"], fact["params"]).fetchall()
    if rows and "g0" in rows[0]:
        keys = sorted(k for k in rows[0] if k.startswith("g"))
        groups = [{"key": [r[k] for k in keys], "value": _num(r["value"])} for r in rows]
        return {"groups": groups, "total": sum(g["value"] or 0 for g in groups)}
    return {"value": _num(rows[0]["value"]) if rows else 0}


def _num(v):
    if v is None:
        return None
    f = float(v)
    return int(f) if f.is_integer() else f


def list_create(job_id: str, handle: str, columns: list[str], filters: list[dict] | None = None,
                label_filter: str | None = None, sort: str | None = None) -> dict:
    rs = recordstore.get_by_handle(job_id, handle)
    where, params = _where(filters or [], label_filter, job_id)
    with db.tx() as c:
        n = c.execute(sql.SQL("SELECT count(*) AS n FROM result_set_members m JOIN records r ON r.id = m.record_id WHERE m.result_set_id = %s AND {}").format(where),
                      [rs["id"], *params]).fetchone()["n"]
        c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"lists:{job_id}",))
        k = c.execute("SELECT count(*) AS n FROM rendered_lists WHERE job_id = %s", (job_id,)).fetchone()["n"] + 1
        row = c.execute("""INSERT INTO rendered_lists (job_id, list_key, result_set_id, spec, row_count)
                           VALUES (%s,%s,%s,%s,%s) RETURNING *""",
                        (job_id, f"L-{k}", rs["id"], db.j({"columns": columns, "filters": filters or [], "label_filter": label_filter, "sort": sort}), n)).fetchone()
    return {"list_key": row["list_key"], "row_count": n, "placeholder": "{{list:%s}}" % row["list_key"]}


def list_rows(job_id: str, list_key: str, limit: int | None = None) -> tuple[list[str], list[dict], int]:
    lst = db.q1("SELECT * FROM rendered_lists WHERE job_id = %s AND list_key = %s", (job_id, list_key))
    if not lst:
        raise AnalyzerError(f"unknown list {list_key}")
    spec = lst["spec"]
    where, params = _where(spec["filters"], spec.get("label_filter"), job_id)
    cols = spec["columns"] or ["source_id", "title", "status"]
    sel = sql.SQL(", ").join(sql.SQL("{} AS {}").format(_field_expr(c), sql.Identifier(c)) for c in cols)
    order = _field_expr(spec["sort"]) if spec.get("sort") else sql.SQL("r.source_system, r.source_id")
    q = sql.SQL("SELECT {sel}, r.source_url AS _url, r.source_system AS _sys, r.source_id AS _id FROM result_set_members m JOIN records r ON r.id = m.record_id "
                "WHERE m.result_set_id = %s AND {w} ORDER BY {o}{lim}").format(
        sel=sel, w=where, o=order, lim=sql.SQL(" LIMIT {}").format(sql.Literal(limit)) if limit else sql.SQL(""))
    with db.tx() as c:
        rows = c.execute(q, [lst["result_set_id"], *params]).fetchall()
    return cols, rows, lst["row_count"]


def export_csv(job_id: str, list_key: str) -> str:
    cols, rows, _ = list_rows(job_id, list_key)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols + ["url"])
    for r in rows:
        w.writerow([r[c] for c in cols] + [r["_url"]])
    return buf.getvalue()
