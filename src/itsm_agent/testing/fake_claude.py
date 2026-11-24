"""A local HTTP server that speaks the Claude Messages API (JSON and SSE streaming) so the real SDK,
LLM gateway, harness and validators run end-to-end without an API key.

Roles are recognised from <role>…</role> in the system prompt:
- orchestrator: a scripted policy that follows the CAB-compliance playbook from tool results
- map_worker / second_opinion / adjudicator: keyword classifier with optional fault injection
- verifier: checks numeric claims against the facts it is shown
This is test infrastructure: it does not imitate model quality, it exercises the system's contracts.
"""
from __future__ import annotations

import hashlib
import html
import json
import random
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NET = re.compile(r"\b(firewall|router|switch|vlan|bgp|dns|subnet|load balancer|vpn)\b", re.I)


def classify(text: str) -> tuple[str, str]:
    title = text.split(". ")[0][:100]
    if NET.search(text) and "printer" not in text.lower():
        return "network_change", title
    return "not_network_change", title


class Brain:
    def __init__(self, faults: dict | None = None, orchestrator_faults: set | None = None):
        self.faults = faults or {}               # e.g. {"drop": .03, "dup": .02, "fake_quote": .03, "flip": .01, "truncate": .02}
        self.orch_faults = orchestrator_faults or set()   # {"wrong_number_first"}
        self.requests: list[dict] = []
        self.lock = threading.Lock()

    # ------------------------------------------------------------------ dispatch
    def respond(self, req: dict) -> tuple[list[dict], str]:
        with self.lock:
            self.requests.append(req)
        system = req.get("system") or ""
        system_text = system if isinstance(system, str) else " ".join(b.get("text", "") for b in system)
        role = (re.search(r"<role>(\w+)</role>", system_text) or [None, "unknown"])[1]
        if role in ("map_worker", "second_opinion"):
            return self._map(req, role)
        if role == "adjudicator":
            return self._adjudicate(req)
        if role == "verifier":
            return self._verify(req)
        if role == "orchestrator":
            return self._orchestrate(req)
        return [{"type": "text", "text": "ok"}], "end_turn"

    # ------------------------------------------------------------------ workers
    def _records(self, req) -> list[tuple[str, str]]:
        content = req["messages"][0]["content"]
        text = content if isinstance(content, str) else " ".join(b.get("text", "") for b in content)
        return [(rid, html.unescape(body)) for rid, body in re.findall(r'<untrusted_record rid="(R\d+)"[^>]*>(.*?)</untrusted_record>', text, re.S)]

    def _map(self, req, role):
        recs = self._records(req)
        seed = int(hashlib.sha256(json.dumps(req["messages"], sort_keys=True).encode()).hexdigest()[:8], 16)
        rnd = random.Random(seed)
        f = self.faults if role == "map_worker" else {}
        if rnd.random() < f.get("truncate", 0):
            return [{"type": "text", "text": '{"results": [{"rid": "R1"'}], "max_tokens"
        out = []
        for rid, text in recs:
            if rnd.random() < f.get("drop", 0):
                continue
            label, ev = classify(text)
            if rnd.random() < f.get("flip", 0):
                label = "not_network_change" if label == "network_change" else "network_change"
                if label == "network_change":
                    ev = "core switch replaced"           # a false positive cannot quote real evidence
            if rnd.random() < f.get("fake_quote", 0):
                ev = "BGP session reset on core"
            item = {"rid": rid, "label": label, "confidence": "high", "evidence_quote": ev,
                    "reason_code": "keyword_explicit" if label == "network_change" else "described_activity"}
            out.append(item)
            if rnd.random() < f.get("dup", 0):
                out.append({**item, "label": "not_network_change" if label == "network_change" else "network_change"})
        if recs and rnd.random() < f.get("invent", 0):
            out.append({"rid": "R999", "label": "network_change", "confidence": "high", "evidence_quote": "x", "reason_code": "keyword_explicit"})
        return [{"type": "text", "text": json.dumps({"results": out})}], "end_turn"

    def _adjudicate(self, req):
        (_, text), = self._records(req)
        label, ev = classify(text)
        return [{"type": "text", "text": json.dumps({"label": label, "evidence_quote": ev, "reason": "rubric applied"})}], "end_turn"

    def _verify(self, req):
        content = req["messages"][0]["content"]
        text = content if isinstance(content, str) else " ".join(b.get("text", "") for b in content)
        facts = {k: json.loads(v) for k, v in re.findall(r'<fact key="(F-\d+)"[^>]*>(.*?)</fact>', text, re.S)}
        verdicts = []
        for cid, ctype, value, refs in re.findall(r'<claim id="([^"]+)" type="(\w+)" value="([^"]*)" refs="([^"]*)">', text):
            refs = [r for r in refs.split(",") if r]
            if ctype == "numeric":
                vals = set()
                for r in refs:
                    fv = facts.get(r, {})
                    vals |= {float(fv[k]) for k in ("value", "total") if fv.get(k) is not None}
                ok = value not in ("", "None") and float(value) in vals
                verdicts.append({"claim_id": cid, "verdict": "supported" if ok else "contradicted", "rule": "A1",
                                 "explanation": "matches fact" if ok else f"fact values {sorted(vals)}"})
            else:
                ok = all(f'ref="{r}"' in text or r in facts for r in refs)
                verdicts.append({"claim_id": cid, "verdict": "supported" if ok else "not_enough_info", "rule": "A1",
                                 "explanation": "evidence present" if ok else "evidence missing"})
        return [{"type": "text", "text": json.dumps({"verdicts": verdicts})}], "end_turn"

    # ------------------------------------------------------------------ orchestrator
    def _orchestrate(self, req):
        msgs = req["messages"]
        first = msgs[0]["content"]
        first_text = first if isinstance(first, str) else " ".join(b.get("text", "") for b in first)
        handoff = None
        m = re.search(r"<handoff>\n(.*?)\n</handoff>", first_text, re.S)
        if m:
            handoff = json.loads(m.group(1))
        question = handoff["last_user_turn"] if handoff else re.search(r"<user_turn>\n(.*?)\n</user_turn>", first_text, re.S).group(1)
        calls: list[tuple[str, dict, str, bool]] = []         # (name, input, result_text, is_error)
        pending: dict[str, tuple[str, dict]] = {}
        for msg in msgs[1:]:
            if msg["role"] == "assistant":
                for b in msg["content"]:
                    if b.get("type") == "tool_use":
                        pending[b["id"]] = (b["name"], b["input"])
            elif isinstance(msg["content"], list):
                for b in msg["content"]:
                    if b.get("type") == "tool_result" and b["tool_use_id"] in pending:
                        name, inp = pending[b["tool_use_id"]]
                        calls.append((name, inp, b["content"] if isinstance(b["content"], str) else json.dumps(b["content"]), bool(b.get("is_error"))))
        done = {}
        for name, inp, res, err in calls:
            if not err:
                try:
                    done.setdefault(name, []).append((inp, json.loads(res)))
                except json.JSONDecodeError:
                    done.setdefault(name, []).append((inp, res))
        state = self._state(handoff, done)
        q = question.lower()
        if "comment" in q or "work note" in q:
            return self._followup(first_text, done, calls)
        if "network" in q:
            return self._cab_playbook(question, state, done, calls)
        return self._tool("submit_answer", {"narrative_md": "I can only answer change and CAB questions in this demo.",
                                            "claims": [], "caveats": ["Question not supported by the scripted demo model."]})

    def _state(self, handoff, done):
        st = {"spec": None, "sn": None, "jira": None, "union": None, "classified": False, "facts": {}, "list": None}
        if handoff:
            st["spec"] = handoff["query_specs"][0]["id"] if handoff.get("query_specs") else None
            for h in handoff.get("handles", []):
                key = "sn" if h["source"] == "servicenow" else "jira" if h["source"] == "jira" else "union" if h["source"].startswith("union") else None
                if key:
                    st[key] = h["handle"]
            for f in handoff.get("facts", []):
                st["facts"][f["what"]] = (f["key"], f["value"])
            st["list"] = handoff["lists"][0]["key"] if handoff.get("lists") else None
            st["classified"] = bool(st["facts"]) or "classified" in json.dumps(handoff.get("steps", {}))
        for inp, res in done.get("plan_query_spec", []):
            st["spec"] = res["query_spec_id"]
        for inp, res in done.get("servicenow_records_search_to_resultset", []):
            st["sn"] = res.get("handle")
        for inp, res in done.get("jira_issues_search_to_resultset", []):
            st["jira"] = res.get("handle")
        for inp, res in done.get("analyzer_resultset_union", []):
            st["union"] = res["handle"]
        if done.get("analyzer_classify"):
            st["classified"] = True
        for inp, res in done.get("analyzer_aggregate", []):
            st["facts"][inp["description"]] = (res["fact_key"], res["value"])
        for inp, res in done.get("analyzer_list_create", []):
            st["list"] = res["list_key"]
        return st

    def _cab_playbook(self, question, st, done, calls):
        lf = "network_change_v3:network_change"
        if not st["spec"]:
            m = re.search(r"(last|past) \d+ (hours|days|minutes|weeks)|yesterday|today", question.lower())
            return self._tool("plan_query_spec", {"intent": question, "relative_window": m.group(0) if m else "last 24 hours",
                                                  "window_field": "closed", "systems": ["servicenow", "jira"], "rubric_id": "network_change_v3"})
        if not st["sn"]:
            return self._tool("servicenow_records_search_to_resultset", {"query_spec_id": st["spec"], "table": "change_request", "encoded_query": "",
                                                                          "fields": ["number", "short_description", "category", "approval"]})
        if not st["jira"]:
            return self._tool("jira_issues_search_to_resultset", {"query_spec_id": st["spec"], "jql": "project in (NET, OPS) AND issuetype = Change",
                                                                  "fields": ["summary", "components", "resolutiondate"]})
        if not st["union"]:
            return self._tool("analyzer_resultset_union", {"handles": [st["sn"], st["jira"]], "dedupe_key": "change_ref"})
        if not st["classified"]:
            return self._tool("analyzer_classify", {"handle": st["union"], "rubric_id": "network_change_v3", "only_undecided": True})
        if "network changes in window" not in st["facts"]:
            return self._tool("analyzer_aggregate", {"handle": st["union"], "metric": "count", "label_filter": lf,
                                                     "description": "network changes in window"})
        if "network changes without approved CAB" not in st["facts"]:
            return self._tool("analyzer_aggregate", {"handle": st["union"], "metric": "count", "label_filter": lf,
                                                     "filters": [{"field": "cab_status", "op": "neq", "value": "approved"}],
                                                     "description": "network changes without approved CAB"})
        if not st["list"]:
            return self._tool("analyzer_list_create", {"handle": st["union"], "columns": ["source_id", "title", "cab_status", "source_system"],
                                                       "filters": [{"field": "cab_status", "op": "neq", "value": "approved"}], "label_filter": lf})
        if "scratchpad_write" not in done:
            return self._tool("scratchpad_write", {"entry_type": "decision", "text": "CAB status taken from ServiceNow approval / Jira CAB field",
                                                   "refs": [], "supersedes_id": None})
        f1, v1 = st["facts"]["network changes in window"]
        f2, v2 = st["facts"]["network changes without approved CAB"]
        total, without = v1["value"], v2["value"]
        submits = [c for c in calls if c[0] == "submit_answer"]
        if "wrong_number_first" in self.orch_faults and not submits:
            without += 1
        return self._tool("submit_answer", {
            "narrative_md": f"In the window there were {{{{{f1}}}}} network changes. {{{{{f2}}}}} of them have no approved CAB record:{{{{list:{st['list']}}}}}",
            "claims": [{"claim_id": "c1", "claim_type": "numeric", "text": "network changes in window", "value": total, "refs": [f1]},
                       {"claim_id": "c2", "claim_type": "numeric", "text": "network changes without approved CAB", "value": without, "refs": [f2]}],
            "caveats": []})

    def _followup(self, first_text, done, calls):
        m = re.search(r"- (ref-\d+): (L-\d+)[^\n]*-> list (L-\d+)", first_text)
        if "referent_resolve" not in done:
            if not m:
                return self._tool("submit_answer", {"narrative_md": "I could not find the earlier list.", "claims": [], "caveats": ["no referent"]})
            return self._tool("referent_resolve", {"phrase": "those changes", "referent_id": m.group(1)})
        ordinal = done["referent_resolve"][0][1]["ordinal_map"]
        proposals = done.get("servicenow_work_note_add", []) + done.get("jira_issue_comment_add", [])
        if not proposals:
            uses = []
            for k in sorted(ordinal, key=int)[:3]:
                system, sid = ordinal[k].split(":", 1)
                if system == "servicenow":
                    uses.append(("servicenow_work_note_add", {"table": "change_request", "sys_id": sid,
                                                              "text": "CAB approval missing for this network change. Please attach CAB approval."}))
                else:
                    uses.append(("jira_issue_comment_add", {"issue_key": sid, "body": "CAB approval missing for this network change.",
                                                            "visibility_group": ""}))
            return self._tools(uses)
        n = len(proposals)
        return self._tool("submit_answer", {"narrative_md": f"I proposed {n} comments on those changes. They are awaiting approval; nothing has been changed yet.",
                                            "claims": [], "caveats": []})

    # ------------------------------------------------------------------ helpers
    _n = 0

    def _tool(self, name, inp):
        return self._tools([(name, inp)])

    def _tools(self, uses):
        blocks = [{"type": "thinking", "thinking": "", "signature": "fake-signature"}]
        for name, inp in uses:
            Brain._n += 1
            blocks.append({"type": "tool_use", "id": f"toolu_fake_{Brain._n:06d}", "name": name, "input": inp})
        return blocks, "tool_use"


class _Handler(BaseHTTPRequestHandler):
    brain: Brain

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        req = json.loads(self.rfile.read(length) or b"{}")
        if not self.path.startswith("/v1/messages"):
            self.send_response(404); self.end_headers(); return
        if self.path.startswith("/v1/messages/count_tokens"):
            return self._json({"input_tokens": len(json.dumps(req)) // 4})
        content, stop = self.brain.respond(req)
        usage = {"input_tokens": len(json.dumps(req)) // 4, "output_tokens": len(json.dumps(content)) // 4,
                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        msg_id = "msg_" + hashlib.sha256(json.dumps(req, sort_keys=True).encode()).hexdigest()[:20]
        if not req.get("stream"):
            return self._json({"id": msg_id, "type": "message", "role": "assistant", "model": req["model"], "content": content,
                               "stop_reason": stop, "stop_sequence": None, "usage": usage})
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("request-id", "req_fake")
        self.end_headers()
        def ev(name, data):
            self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
        ev("message_start", {"type": "message_start", "message": {"id": msg_id, "type": "message", "role": "assistant", "model": req["model"],
                                                                   "content": [], "stop_reason": None, "stop_sequence": None,
                                                                   "usage": {**usage, "output_tokens": 1}}})
        for i, b in enumerate(content):
            if b["type"] == "text":
                ev("content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}})
                ev("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": b["text"]}})
            elif b["type"] == "thinking":
                ev("content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
                ev("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "signature_delta", "signature": b["signature"]}})
            else:
                ev("content_block_start", {"type": "content_block_start", "index": i,
                                           "content_block": {"type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}})
                ev("content_block_delta", {"type": "content_block_delta", "index": i,
                                           "delta": {"type": "input_json_delta", "partial_json": json.dumps(b["input"])}})
            ev("content_block_stop", {"type": "content_block_stop", "index": i})
        ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                             "usage": {"output_tokens": usage["output_tokens"]}})
        ev("message_stop", {"type": "message_stop"})

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.send_header("request-id", "req_fake")
        self.end_headers()
        self.wfile.write(body)


class FakeClaudeServer:
    def __init__(self, brain: Brain | None = None):
        self.brain = brain or Brain()
        handler = type("H", (_Handler,), {"brain": self.brain})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> "FakeClaudeServer":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
