#!/usr/bin/env python3
"""Doc integrity checks for ARCHITECTURE.md, README.md and docs/*.md.

  K1 every relative link resolves to a file (and anchor, if given)
  K2 every cross-reference "doc NN §x" / "ARCHITECTURE §x" points to an existing doc and section
  K3 no text diagrams: no mermaid/plantuml blocks, no box-drawing or arrow-flow code blocks
  K4 every schema test id cited (T1.1, T14.3 ...) exists in schema/seed_and_tests.sql
  K5 every `tool_name` with a server prefix exists in contracts/tools; harness tools in contracts/harness_tools.json
  K6 every image embedded exists as PNG with an SVG source and a generator function; every diagram is used
  K7 every explanation doc is linked from ARCHITECTURE.md and links back to it
  K8 context budgets agree between ARCHITECTURE §7.2 and doc 01 §3.1, the diagram source, and sum to 120k
"""
import json, re, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
arch = ROOT / "ARCHITECTURE.md"
docs = {int(p.name[:2]): p for p in (ROOT / "docs").glob("[0-9][0-9]-*.md")}
files = [arch, ROOT / "README.md"] + sorted(docs.values())
fails = []

def slug(h):
    return re.sub(r"[^\w\- ]", "", h.strip().lower()).replace(" ", "-")

def headings(p):
    return [l.lstrip("#").strip() for l in p.read_text().splitlines() if l.startswith("#")]

def section_numbers(p):
    nums = set()
    for h in headings(p):
        m = re.match(r"(?:[①-⑦]\s*)?(\d+(?:\.\d+)*[a-z]?)[.\s]", h + " ")
        if m:
            nums.add(m.group(1))
    return nums

contract_tools, servers = set(), set()
for cf in (ROOT / "contracts/tools").glob("*.json"):
    c = json.loads(cf.read_text()); servers.add(c["server"])
    contract_tools |= {t["name"] for t in c["tools"]}
harness_tools = {t["name"] for t in json.loads((ROOT / "contracts/harness_tools.json").read_text())["tools"]}
test_ids = set(re.findall(r"'(T\d+\.\d+b?)\s", (ROOT / "schema/seed_and_tests.sql").read_text()))
gen_src = (ROOT / "diagrams/src/diagrams.py").read_text()
used_images = set()
counts = dict(links=0, refs=0, images=0, tools=0)

for f in files:
    text = f.read_text()
    body = re.sub(r"```.*?```", "", text, flags=re.S)
    for target in re.findall(r"\]\(([^)]+)\)", body):
        if target.startswith("http"):
            continue
        counts["links"] += 1
        path, _, anchor = target.partition("#")
        dest = (f.parent / path).resolve() if path else f
        if not dest.exists():
            fails.append(f"K1 {f.name}: broken link {target}")
        elif anchor and dest.suffix == ".md" and anchor not in {slug(h) for h in headings(dest)}:
            fails.append(f"K1 {f.name}: missing anchor {target}")
        if dest.suffix == ".png":
            counts["images"] += 1
            used_images.add(dest.stem)
            if not dest.with_suffix(".svg").exists():
                fails.append(f"K6 {f.name}: {dest.name} has no SVG source")
            if f"def {dest.stem}(" not in gen_src:
                fails.append(f"K6 {f.name}: {dest.stem} has no generator in diagrams/src/diagrams.py")
    for num, sec in re.findall(r"\b[Dd]ocs? (\d{2})(?: §(\d+(?:\.\d+)*[a-z]?))?", body):
        counts["refs"] += 1
        d = int(num)
        if d not in docs:
            fails.append(f"K2 {f.name}: reference to missing doc {num}")
        elif sec and sec not in section_numbers(docs[d]):
            fails.append(f"K2 {f.name}: doc {num} has no section §{sec}")
    for sec in re.findall(r"ARCHITECTURE(?:\.md)? §(\d+(?:\.\d+)*)", body):
        counts["refs"] += 1
        if sec not in section_numbers(arch):
            fails.append(f"K2 {f.name}: ARCHITECTURE has no section §{sec}")
    for lang, block in re.findall(r"```(\w*)\n(.*?)```", text, flags=re.S):
        if lang.lower() in {"mermaid", "plantuml", "dot", "graphviz"}:
            fails.append(f"K3 {f.name}: {lang} text diagram (use a rendered image)")
        if re.search(r"[┌┐└┘│─├┤▶►]|-->|==>", block) or re.search(r"^\s*(→|->)", block, re.M):
            fails.append(f"K3 {f.name}: code block looks like a text diagram")
    for tid in re.findall(r"\b(T\d+\.\d+b?)\b", body):
        if tid not in test_ids:
            fails.append(f"K4 {f.name}: cites unknown schema test {tid}")
    for tok in re.findall(r"`([a-z][a-z0-9_]+)`", text):
        pref = next((sv for sv in servers if tok.startswith(sv + "_")), None)
        if pref and not tok.endswith(("_v3", "_tz")):
            counts["tools"] += 1
            if tok not in contract_tools:
                fails.append(f"K5 {f.name}: unknown tool `{tok}`")

all_diagrams = {p.stem for p in (ROOT / "diagrams").glob("*.png")}
for unused in sorted(all_diagrams - used_images):
    fails.append(f"K6 diagram {unused} is not used in any document")
for missing in re.findall(r"^def (\w+)\(\):", gen_src, re.M):
    if missing not in all_diagrams:
        fails.append(f"K6 generator {missing} has not been rendered")

arch_text = arch.read_text()
for n, p in docs.items():
    if f"docs/{p.name}" not in arch_text:
        fails.append(f"K7 {p.name} is not linked from ARCHITECTURE.md")
    if "ARCHITECTURE" not in p.read_text():
        fails.append(f"K7 {p.name} does not refer back to ARCHITECTURE.md")

def budgets(text):
    out = {}
    for layer, val in re.findall(r"^\| (L\d|R)\s*\|[^|]*\|\s*(?:\*\*)?([≤≥] ?\d+)k", text, re.M):
        out[layer] = int(re.sub(r"\D", "", val))
    return out
b_arch = budgets(arch_text.split("### 7.2", 1)[1].split("### 7.3", 1)[0])
b_doc1 = budgets(docs[1].read_text().split("### 3.1", 1)[1].split("### 3.2", 1)[0])
b_diag = {m[0].split()[0]: int(m[1]) for m in re.findall(r'\("((?:L\d|R)\s+[^"]+)", "[^"]*", "[≤≥] (\d+)k"', gen_src)}
if not b_arch or b_arch != b_doc1 or b_arch != b_diag:
    fails.append(f"K8 budget mismatch: ARCHITECTURE {b_arch} / doc01 {b_doc1} / diagram {b_diag}")
elif sum(b_arch.values()) != 120:
    fails.append(f"K8 budgets sum to {sum(b_arch.values())}k, expected 120k")

print(f"budget_layers={len(b_arch)} budget_total={sum(b_arch.values())}k")
print(f"files={len(files)} links={counts['links']} images={counts['images']} cross_refs={counts['refs']} "
      f"tool_mentions={counts['tools']} diagrams={len(all_diagrams)} schema_test_ids={len(test_ids)}")
for x in fails:
    print("FAIL", x)
print("DOCS OK" if not fails else f"{len(fails)} FAILURE(S)")
sys.exit(1 if fails else 0)
