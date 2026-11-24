"""Custom tool router (docs/06 §2): picks the epoch's tool set by role, relevance and an 8k-token budget."""
from __future__ import annotations

import re

from .registry import api_tool, contract, enabled_tools, harness_contracts

CORE = ["plan_query_spec", "ledger_update", "scratchpad_write", "request_tools", "referent_resolve", "memory_search",
        "memory_propose", "submit_answer", "analyzer_resultset_describe", "analyzer_aggregate", "analyzer_list_create",
        "analyzer_records_sample", "analyzer_resultset_filter", "analyzer_resultset_union", "analyzer_classify"]
BUDGET_TOKENS = 8000


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z]{3,}", text.lower()))


def _cost(name: str) -> int:
    import json
    return int(len(json.dumps(api_tool(name), sort_keys=True)) / 3.5) + 10


def select(role: str, goal: str, extra_capability: str | None = None, previous: list[str] | None = None) -> list[str]:
    """Returns tool names, sorted, frozen for the epoch."""
    pool = {t["name"]: t for t in enabled_tools() if role in t["allowed_roles"]}
    pool.update({n: t for n, t in harness_contracts().items() if role in t["roles"]})
    chosen = [n for n in CORE if n in pool]
    chosen += [n for n in (previous or []) if n in pool and n not in chosen]
    words = _tokens(goal + " " + (extra_capability or ""))
    ranked = sorted((n for n in pool if n not in chosen),
                    key=lambda n: -len(words & _tokens(n.replace("_", " ") + " " + contract(n)["description"])))
    budget = BUDGET_TOKENS - sum(_cost(n) for n in chosen)
    for n in ranked:
        c = _cost(n)
        if c <= budget:
            chosen.append(n)
            budget -= c
    return sorted(chosen)


def find_for_capability(role: str, capability: str, current: list[str]) -> list[str]:
    words = _tokens(capability)
    pool = [t["name"] for t in enabled_tools() if role in t["allowed_roles"] and t["name"] not in current]
    return [n for n in pool if words & _tokens(n.replace("_", " ") + " " + contract(n)["description"])]
