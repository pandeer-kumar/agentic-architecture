"""Payload builder (docs/02): durable state -> budgeted, fenced, deterministically ordered request.

The epoch's base (tools, system, first user message) is built once when the epoch opens and then frozen;
later turns only append (the append-only rule, docs/01 §4). Every request is snapshotted."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from importlib import resources

from .. import db
from ..config import get_settings
from ..memory import ltm, stm
from ..tools.registry import api_tool

BUDGETS = {"L0": 8000, "L1": 4000, "L2": 3000, "L3": 6000, "L4": 3000, "L5": 3000, "L6": 3000, "L7": 3000, "L9": 2000}


def est_tokens(text: str) -> int:
    return max(1, int(len(text) / 3.5))


def system_core() -> str:
    text = resources.files("itsm_agent.prompts").joinpath("orchestrator.md").read_text()
    return f'<system_core version="{hashlib.sha256(text.encode()).hexdigest()[:10]}">\n{text}\n</system_core>'


def _fit(lines: list[str], budget: int, header: str, footer: str) -> str:
    """Keep lines in order until the layer budget is reached; mark what was dropped."""
    out, used = [], est_tokens(header + footer)
    for i, ln in enumerate(lines):
        t = est_tokens(ln)
        if used + t > budget:
            out.append(f"[{len(lines) - i} more items omitted for budget]")
            break
        out.append(ln)
        used += t
    return header + "\n".join(out) + footer


@dataclass
class EpochBase:
    tools: list[dict]
    system: list[dict]
    first_message: dict
    blocks: list[dict] = field(default_factory=list)      # for the snapshot manifest
    tokens: int = 0


def select_playbook(goal: str) -> dict | None:
    words = {w for w in goal.lower().split() if len(w) > 3}
    best, score = None, 0
    for p in db.q("SELECT name, version, description, body_md FROM playbooks WHERE status = 'active'"):
        s = len(words & set((p["description"] + " " + p["name"].replace("_", " ")).lower().split()))
        if s > score:
            best, score = p, s
    return best if score >= 1 else None


def build_epoch_base(job: dict, session: dict, user_id: str, tool_names: list[str], user_turn: str,
                     handoff: dict | None) -> EpochBase:
    blocks: list[dict] = []

    def add(pos, btype, layer, text, cache=False):
        if est_tokens(text) > BUDGETS.get(layer, 10 ** 9) * 1.05:
            raise ValueError(f"block {btype} exceeds layer {layer} budget")
        blocks.append({"pos": pos, "block_type": btype, "layer": layer, "text": text, "tokens": est_tokens(text),
                       "cache_mark": cache or None})
        return text

    tools = [api_tool(n) for n in sorted(tool_names)]
    add("tools", "tool_defs", "L0", json.dumps(tools, sort_keys=True))
    goal = (handoff or {}).get("goal") or user_turn
    pb = select_playbook(goal)
    system = [{"type": "text", "text": add("system[0]", "system_core", "L1", system_core())},
              {"type": "text", "text": add("system[1]", "org_profile", "L2",
                                           f"<org_profile>\n{ltm.pinned_profile() or '(none)'}\n</org_profile>")}]
    pb_text = f'<playbook name="{pb["name"]}" version="{pb["version"]}">\n{pb["body_md"]}\n</playbook>' if pb else "<playbook>(none selected)</playbook>"
    system.append({"type": "text", "text": add("system[2]", "playbook", "L3", pb_text, cache=True), "cache_control": {"type": "ephemeral"}})

    st = db.q1("SELECT summary, user_tz FROM session_state WHERE session_id = %s", (session["id"],))
    refs = stm.recent_referents(str(session["id"]))
    l4 = _fit([f'- {r["referent_key"]}: {r["label"]} -> {r["kind"]} {r["target"]}' for r in refs], BUDGETS["L4"] - 800,
              f"<session_state tz=\"{st['user_tz'] if st else 'UTC'}\">\n{(st or {}).get('summary') or '(new session)'}\n</session_state>\n<referents>\n",
              "\n</referents>")
    ledger = db.q1("SELECT ledger FROM jobs WHERE id = %s", (job["id"],))["ledger"]
    l5 = _fit([json.dumps(x, sort_keys=True, default=str) for x in ledger.get("handles", []) + ledger.get("facts", []) + ledger.get("lists", [])],
              BUDGETS["L5"], "<task_ledger>\n" + json.dumps({k: v for k, v in ledger.items() if k not in ("handles", "facts", "lists")},
                                                           sort_keys=True, default=str) + "\n", "\n</task_ledger>")
    sp = stm.active_scratchpad(str(job["id"]))
    l6 = _fit([f'- sp:{e["id"]} [{e["entry_type"]}] {e["text"]} refs={",".join(e["refs"])}' for e in sp], BUDGETS["L6"],
              "<scratchpad>\n", "\n</scratchpad>")
    mem = ltm.search(goal, user_id, limit=8)
    l7 = _fit([f'<memory id="{m["id"]}" v="{m["version"]}" kind="{m["kind"]}" confidence="{float(m["confidence"]):.2f}" '
               f'as_of="{m["as_of"]:%Y-%m-%d}">{m["content"]}</memory>' for m in mem], BUDGETS["L7"], "<retrieved_memory>\n", "\n</retrieved_memory>")
    l9 = (f"<handoff>\n{json.dumps(handoff, sort_keys=True, default=str)}\n</handoff>\nContinue the task from the handoff."
          if handoff else f"<user_turn>\n{user_turn}\n</user_turn>")
    content = [{"type": "text", "text": add("messages[0]", "session_state", "L4", l4)},
               {"type": "text", "text": add("messages[0]", "ledger", "L5", l5)},
               {"type": "text", "text": add("messages[0]", "scratchpad", "L6", l6)},
               {"type": "text", "text": add("messages[0]", "retrieved_memory", "L7", l7)},
               {"type": "text", "text": add("messages[0]", "handoff" if handoff else "user_turn", "L9", l9, cache=True),
                "cache_control": {"type": "ephemeral"}}]
    return EpochBase(tools=tools, system=system, first_message={"role": "user", "content": content}, blocks=blocks,
                     tokens=sum(b["tokens"] for b in blocks))


def turn_blocks(base: EpochBase, messages: list[dict]) -> list[dict]:
    """Snapshot manifest for one request: frozen base blocks + every appended L8 message."""
    extra = [{"pos": f"messages[{i}]", "block_type": "epoch_turn", "layer": "L8", "text": db.dumps(m),
              "tokens": est_tokens(db.dumps(m))} for i, m in enumerate(messages[1:], start=1)]
    return base.blocks + extra


def working_set_tokens(messages: list[dict]) -> int:
    return sum(est_tokens(db.dumps(m)) for m in messages[1:])


def hard_limit() -> int:
    return get_settings().orchestrator_budget_tokens
