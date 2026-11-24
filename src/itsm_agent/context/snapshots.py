"""Content-addressed blocks + per-request manifests (docs/02 §7): any request can be replayed exactly."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .. import db
from ..config import REPO_ROOT

INLINE_MAX = 8192
BLOB_DIR = Path(__import__("os").environ.get("ITSM_BLOB_DIR", str(REPO_ROOT / ".blobs")))


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def put_blob(c, text: str) -> str:
    h = sha(text)
    size = len(text.encode("utf-8"))
    if size <= INLINE_MAX:
        c.execute("""INSERT INTO content_blobs (content_hash, byte_size, storage, inline_content) VALUES (%s,%s,'inline',%s)
                     ON CONFLICT (content_hash) DO UPDATE SET last_used_at = now()""", (h, size, text))
    else:
        path = BLOB_DIR / h[:2] / h
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        c.execute("""INSERT INTO content_blobs (content_hash, byte_size, storage, blob_uri) VALUES (%s,%s,'blob',%s)
                     ON CONFLICT (content_hash) DO UPDATE SET last_used_at = now()""", (h, size, path.as_uri()))
    return h


def get_blob(h: str) -> str:
    row = db.q1("SELECT * FROM content_blobs WHERE content_hash = %s", (h,))
    if row is None:
        raise KeyError(h)
    if row["storage"] == "inline":
        return row["inline_content"]
    return Path(row["blob_uri"].removeprefix("file://")).read_text(encoding="utf-8")


def save_snapshot(job_id: str, epoch_id: str | None, role: str, turn: int, request: dict, blocks: list[dict],
                  token_estimate: int) -> str:
    """blocks: [{pos, block_type, layer, text, tokens, cache_mark?}]; the request is also stored whole."""
    req_text = db.dumps(request)
    with db.tx() as c:
        manifest = []
        for b in blocks:
            h = put_blob(c, b["text"])
            manifest.append({k: v for k, v in {"pos": b["pos"], "block_type": b["block_type"], "layer": b["layer"],
                                               "content_hash": h, "tokens": b["tokens"], "cache_mark": b.get("cache_mark")}.items() if v is not None})
        rh = put_blob(c, req_text)
        manifest.append({"pos": "request", "block_type": "request", "layer": "-", "content_hash": rh, "tokens": token_estimate})
        row = c.execute("""INSERT INTO context_snapshots (job_id, epoch_id, agent_role, turn, model_params, manifest, token_estimate, request_hash)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (job_id, epoch_id, role, turn, db.j({k: request.get(k) for k in ("model", "max_tokens", "thinking", "output_config")}),
                         db.j(manifest), max(1, token_estimate), sha(req_text))).fetchone()
    return str(row["id"])


def replay_request(snapshot_id: str) -> dict:
    """Rebuild the exact request that was sent (docs/02 §8)."""
    import json
    snap = db.q1("SELECT * FROM context_snapshots WHERE id = %s", (snapshot_id,))
    req = next(m for m in snap["manifest"] if m["block_type"] == "request")
    text = get_blob(req["content_hash"])
    assert sha(text) == snap["request_hash"], "request hash mismatch"
    return json.loads(text)
