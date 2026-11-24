#!/usr/bin/env python3
"""Regenerate the tool tables in docs/06 (§6.1-6.11) from contracts/ so docs never drift."""
import json, pathlib, re
ROOT = pathlib.Path(__file__).resolve().parent.parent
ORDER = ["jira", "zendesk", "sharepoint", "postgres", "genie", "analyzer", "confluence", "servicenow", "teams", "azure_monitor"]
first = lambda d: re.split(r"(?<!e\.g)(?<!i\.e)\.\s", d, maxsplit=1)[0].rstrip(".") + "."
out = ["## 6. Tools by server", ""]
for i, n in enumerate(ORDER, 1):
    s = json.loads((ROOT / f"contracts/tools/{n}.json").read_text())
    status = s.get("implementation", "phase 2 (contract only)")
    out += [f"### 6.{i} `{n}`", "", f"- **Build:** {s['provenance']}", f"- **Implementation status:** {status}",
            f"- **Transport:** {s['transport']} · **Auth:** {s['auth']}", f"- **Rate limiting:** {s['rate_limit']}", "",
            "| Tool | Risk | Bulk → record store | Key inputs | Purpose |", "|---|---|---|---|---|"]
    for t in s["tools"]:
        ks = ", ".join(f"`{k}`" for k in t["input_schema"]["properties"]) or "—"
        out.append(f"| `{t['name']}` | {t['risk_tier']} | {'yes' if t['bulk_to_resultset'] else ''} | {ks} | {first(t['description'])} |")
    out.append("")
h = json.loads((ROOT / "contracts/harness_tools.json").read_text())
out += ["### 6.11 Harness-native tools", "", "Not MCP; implemented inside the agent runtime.", "",
        "| Tool | Roles | Purpose |", "|---|---|---|"]
out += [f"| `{t['name']}` | {', '.join(t['roles'])} | {first(t['description'])} |" for t in h["tools"]] + [""]
p = ROOT / "docs/06-tool-routing-and-mcp-servers.md"
d = p.read_text()
d = d[:d.index("## 6. Tools by server")] + "\n".join(out) + "\n" + d[d.index("## 7."):]
p.write_text(d)
print("docs/06 tool tables regenerated")
