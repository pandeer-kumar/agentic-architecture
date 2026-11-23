#!/usr/bin/env python3
"""Validate contracts/tools/*.json and their sync with docs/07.

Checks:
  C1 every input_schema is valid JSON Schema (Draft 2020-12)
  C2 strict-tool compatibility: every object has additionalProperties:false,
     required ⊆ properties, no keywords unsupported by structured outputs
  C3 tool names: snake_case, globally unique, prefixed by their server namespace
  C4 risk tier valid; write tiers carry readOnlyHint=false; read tiers readOnlyHint=true
  C5 bulk tools are named *_to_resultset and are read tier
  C6 docs/07 tool tables list exactly the contract tools (no drift)
  C7 every example input in docs/07 §5 validates against its tool schema
  C8 exactly 10 servers
Exit code 0 only if every check passes.
"""
import json, re, sys, pathlib
from jsonschema import Draft202012Validator

ROOT = pathlib.Path(__file__).resolve().parent.parent
UNSUPPORTED = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
               "minLength", "maxLength", "pattern", "minItems", "maxItems", "uniqueItems"}
TIERS = {"read", "low", "medium", "high", "critical"}
failures = []

def fail(msg):
    failures.append(msg)

def walk(schema, path, tool):
    if not isinstance(schema, dict):
        return
    bad = UNSUPPORTED & set(schema)
    if bad:
        fail(f"C2 {tool}{path}: unsupported keywords {sorted(bad)}")
    if schema.get("type") == "object":
        if schema.get("additionalProperties") is not False:
            fail(f"C2 {tool}{path}: object without additionalProperties:false")
        props = schema.get("properties", {})
        missing = set(schema.get("required", [])) - set(props)
        if missing:
            fail(f"C2 {tool}{path}: required not in properties {sorted(missing)}")
        for k, v in props.items():
            walk(v, f"{path}.{k}", tool)
    if schema.get("type") == "array":
        walk(schema.get("items"), f"{path}[]", tool)

tools = {}
servers = sorted((ROOT / "contracts/tools").glob("*.json"))
if len(servers) != 10:
    fail(f"C8 expected 10 servers, found {len(servers)}")
for f in servers:
    s = json.loads(f.read_text())
    ns = s["server"]
    if f.stem != ns:
        fail(f"C3 file {f.name} declares server {ns}")
    for t in s["tools"]:
        n = t["name"]
        if n in tools:
            fail(f"C3 duplicate tool name {n}")
        tools[n] = t
        if not re.fullmatch(r"[a-z][a-z0-9_]{2,63}", n):
            fail(f"C3 bad tool name {n}")
        if not n.startswith(ns + "_"):
            fail(f"C3 {n} not prefixed with namespace {ns}_")
        try:
            Draft202012Validator.check_schema(t["input_schema"])
        except Exception as e:
            fail(f"C1 {n}: invalid schema: {e}")
        walk(t["input_schema"], "", n)
        tier, ann = t["risk_tier"], t["annotations"]
        if tier not in TIERS:
            fail(f"C4 {n}: bad tier {tier}")
        if tier == "read" and ann.get("readOnlyHint") is not True:
            fail(f"C4 {n}: read tier but readOnlyHint is not true")
        if tier in {"medium", "high", "critical"} and ann.get("readOnlyHint") is not False:
            fail(f"C4 {n}: write tier but readOnlyHint is not false")
        if tier == "critical" and ann.get("destructiveHint") is not True:
            fail(f"C4 {n}: critical tier should be destructiveHint=true")
        if t["bulk_to_resultset"] != n.endswith("_to_resultset"):
            fail(f"C5 {n}: bulk flag and _to_resultset suffix disagree")
        if t["bulk_to_resultset"] and tier != "read":
            fail(f"C5 {n}: bulk tools must be read tier")

doc = (ROOT / "docs/07-mcp-servers-and-tools.md").read_text()
section3 = doc.split("## 3. Tools by server", 1)[1].split("## 4.", 1)[0]
doc_tools = set(re.findall(r"^\| `([a-z0-9_]+)` \|", section3, re.M))
if doc_tools != set(tools):
    fail(f"C6 doc/contract drift: only in doc {sorted(doc_tools - set(tools))}, only in contracts {sorted(set(tools) - doc_tools)}")

section5 = doc.split("## 5.", 1)[1].split("## 6.", 1)[0]
examples = re.findall(r"^\| `([a-z0-9_]+)` \| `(\{.*\})` \|$", section5, re.M)
if not examples:
    fail("C7 no examples found in docs/07 §5")
for name, raw in examples:
    try:
        inst = json.loads(raw)
    except json.JSONDecodeError as e:
        fail(f"C7 example for {name} is not valid JSON: {e}")
        continue
    if name not in tools:
        fail(f"C7 example for unknown tool {name}")
        continue
    errs = list(Draft202012Validator(tools[name]["input_schema"]).iter_errors(inst))
    for e in errs:
        fail(f"C7 example for {name}: {e.message}")

print(f"servers={len(servers)} tools={len(tools)} doc_tools={len(doc_tools)} examples={len(examples)}")
for m in failures:
    print("FAIL", m)
print("CONTRACTS OK" if not failures else f"{len(failures)} FAILURE(S)")
sys.exit(1 if failures else 0)
