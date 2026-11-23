#!/usr/bin/env python3
"""Doc integrity checks:
  K1 every relative markdown link resolves to a file (and anchor, if given)
  K2 every cross-reference "doc NN" / "doc NN §x.y" points to an existing doc and section
  K3 every ```mermaid block is extracted to validation/out/ for rendering (render_mermaid.sh)
  K4 every schema test id cited in docs (T1.1, T7.x ...) exists in seed_and_tests.sql
  K5 every `tool_name` mentioned in docs that starts with a server namespace exists in contracts
  K6 every `table` mentioned in docs as a DB object exists in schema.sql
"""
import json
import re, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
docs = {int(p.name[:2]): p for p in (ROOT / "docs").glob("[0-9][0-9]-*.md")}
files = [ROOT / "README.md"] + sorted(docs.values())
fails = []

def slug(h):
    h = h.strip().lower()
    h = re.sub(r"[^\w\- ]", "", h)
    return h.replace(" ", "-")

def headings(p):
    return [l.lstrip("#").strip() for l in p.read_text().splitlines() if l.startswith("#")]

def section_numbers(p):
    nums = set()
    for h in headings(p):
        m = re.match(r"(\d+(?:\.\d+)*)[.\s]", h + " ")
        if m:
            nums.add(m.group(1))
    return nums

out = ROOT / "validation/out"; out.mkdir(exist_ok=True)
for old in out.glob("*.mmd"):
    old.unlink()
n_mermaid = n_links = n_refs = 0
tests_sql = (ROOT / "schema/seed_and_tests.sql").read_text()
test_ids = set(re.findall(r"'(T\d+\.\d+b?)\s", tests_sql))

contract_tools = set()
servers = set()
for cf in (ROOT / "contracts/tools").glob("*.json"):
    c = json.loads(cf.read_text()); servers.add(c["server"])
    contract_tools |= {t["name"] for t in c["tools"]}
NOT_TOOLS = {"jira_tz"}                                   # field names that share a prefix
schema_sql = (ROOT / "schema/schema.sql").read_text()
tables = set(re.findall(r"CREATE TABLE (\w+)", schema_sql))
funcs = set(re.findall(r"FUNCTION itsm\.(\w+)", schema_sql))
TABLE_HINT = re.compile(r"`([a-z_]+)`(?= (?:table|row|rows|column)|\.(?:[a-z_]+)`)")
n_tool_mentions = n_table_mentions = 0

for f in files:
    text = f.read_text()
    for tok in re.findall(r"`([a-z][a-z0-9_]+)`", text):
        pref = next((sv for sv in servers if tok.startswith(sv + "_")), None)
        if pref and tok not in NOT_TOOLS and not tok.endswith("_v3"):
            n_tool_mentions += 1
            if tok not in contract_tools:
                fails.append(f"K5 {f.name}: unknown tool `{tok}`")
    for tok in re.findall(r"`([a-z_]+)`", text):
        if tok in tables or tok in funcs:
            n_table_mentions += 1
    for tok in re.findall(r"`([a-z_]+)\.[a-z_]+`", text):
        if tok not in tables and tok not in {"output_config", "thinking", "response", "usage", "jobs", "records"} and "_" in tok:
            if tok.endswith(("_items", "_sets", "_steps", "_events", "_tools", "_servers", "_calls", "_annotations", "_specs")):
                fails.append(f"K6 {f.name}: unknown table `{tok}`")
    body = re.sub(r"```.*?```", "", text, flags=re.S)          # ignore code blocks for links/refs
    for target in re.findall(r"\]\(([^)]+)\)", body):
        if target.startswith("http"):
            continue
        n_links += 1
        path, _, anchor = target.partition("#")
        dest = (f.parent / path).resolve() if path else f
        if not dest.exists():
            fails.append(f"K1 {f.name}: broken link {target}")
        elif anchor and dest.suffix == ".md" and anchor not in {slug(h) for h in headings(dest)}:
            fails.append(f"K1 {f.name}: missing anchor {target}")
    for num, sec in re.findall(r"\bdocs? (\d{2})(?: §(\d+(?:\.\d+)*))?", body):
        n_refs += 1
        d = int(num)
        if d not in docs:
            fails.append(f"K2 {f.name}: reference to missing doc {num}")
        elif sec and sec not in section_numbers(docs[d]):
            fails.append(f"K2 {f.name}: doc {num} has no section §{sec}")
    for tid in re.findall(r"\b(T\d+\.\d+b?)\b", body):
        if tid not in test_ids:
            fails.append(f"K4 {f.name}: cites unknown schema test {tid}")
    for i, block in enumerate(re.findall(r"```mermaid\n(.*?)```", text, flags=re.S)):
        (out / f"{f.stem}_{i}.mmd").write_text(block)
        n_mermaid += 1

print(f"files={len(files)} links={n_links} cross_refs={n_refs} mermaid_blocks={n_mermaid} schema_test_ids={len(test_ids)} tool_mentions={n_tool_mentions} db_object_mentions={n_table_mentions}")
for x in fails:
    print("FAIL", x)
print("DOCS OK" if not fails else f"{len(fails)} FAILURE(S)")
sys.exit(1 if fails else 0)
