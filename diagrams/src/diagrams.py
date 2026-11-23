"""All architecture diagrams. Run: python3 diagrams/src/diagrams.py && node diagrams/src/render.js"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
from kit import Diagram, Sequence

OUT = os.path.join(os.path.dirname(__file__), "..")
REG = {}
def diagram(fn):
    REG[fn.__name__] = fn
    return fn

# ---------------------------------------------------------------- 01
@diagram
def arch_01_system_context():
    d = Diagram(1500, 780, "System context", "Who uses the ITSM agent platform and which systems it talks to")
    d.group(30, 90, 300, 640, "People & channels", "user")
    for i, (t, l) in enumerate([("IT operations engineer", "Web console / Teams"), ("Service-desk agent", "Web console / Teams"),
                                ("Change manager / approver", "Approval cards"), ("Platform admin", "Admin console"),
                                ("Scheduled reports", "Cron jobs"), ("Other systems", "REST API")]):
        d.box(f"u{i}", 55, 130 + i * 98, 250, 72, t, [l], "user")
    d.group(420, 90, 640, 640, "ITSM agent platform (self-hosted, custom-built)", "agent")
    d.box("core", 470, 150, 540, 110, "Agent runtime", ["custom harness · orchestrator + sub-agents", "custom context engine · payload builder"], "agent", size=17)
    d.box("mem", 470, 290, 260, 100, "Memory service", ["short-term · scratchpad", "long-term (reviewed)"], "memory")
    d.box("tools", 750, 290, 260, 100, "Tool layer", ["tool router · tool gateway", "10 custom MCP servers"], "tool")
    d.box("data", 470, 420, 260, 100, "Data plane", ["record store · analyzer", "verifier engine · renderer"], "data")
    d.box("llm", 750, 420, 260, 100, "LLM gateway", ["routing · retries · fallback", "rate limits · cost metering"], "agent")
    d.box("pg", 470, 560, 260, 120, "PostgreSQL + pgvector", ["system of record", "events · records · memory"], "data", shape="cyl")
    d.box("vault", 750, 560, 120, 120, "Vault", ["secrets"], "sec", shape="cyl")
    d.box("blob", 890, 560, 120, 120, "Blob store", ["payloads", "exports"], "data", shape="cyl")
    d.group(1150, 90, 320, 640, "External systems", "ext")
    d.box("claude", 1175, 130, 270, 70, "Claude Messages API", ["model inference only"], "ext")
    for i, (t, l) in enumerate([("Jira Cloud", "changes · incidents"), ("Zendesk", "customer tickets"), ("ServiceNow", "CMDB · approvals"),
                                ("SharePoint / Teams", "MS Graph"), ("Confluence", "knowledge base"), ("Databricks Genie · Postgres", "analytics"),
                                ("Azure Monitor", "activity log · KQL")]):
        d.box(f"x{i}", 1175, 222 + i * 70, 270, 56, t, [l], "ext", size=13)
    for i in range(6):
        d.arrow(f"u{i}", "core", fs="r", ts="l", to=0.5, via=[(370, 166 + i * 98), (370, 205), ], color="#0284C7")
    d.arrow("llm", "claude", fs="r", ts="l", via=[(1095, 470), (1095, 165)], label="inference", color="#4F46E5", lseg=1, ldx=0)
    d.line([(1010, 340), (1130, 340)], None, color="#D97706")
    d.back.append('<path d="M1140,228 L1132,228 L1132,692 L1140,692" fill="none" stroke="#D97706" stroke-width="2"/>')
    d.text(1125, 520, "REST via", 11, "end", 600, "#D97706"); d.text(1125, 534, "custom MCP", 11, "end", 600, "#D97706")
    d.text(745, 755, "Credentials are injected by the tool gateway from Vault; they never reach the model or model-generated code.", 12, "middle", 600, "#DC2626", True)
    return d

# ---------------------------------------------------------------- 02
@diagram
def arch_02_components():
    d = Diagram(1760, 1130, "Component architecture", "One architecture, seven custom-built subsystems (numbers = sections of ARCHITECTURE.md)")
    d.group(25, 90, 200, 560, "Channels", "user")
    for i, t in enumerate(["Web console", "Teams bot", "Scheduler", "REST API"]):
        d.box(f"c{i}", 45, 130 + i * 120, 160, 70, t, tone="user")
    d.group(250, 90, 230, 560, "Control plane", "ext")
    d.box("gw", 270, 130, 190, 70, "API gateway", ["OIDC auth · quotas"], "ext")
    d.box("sess", 270, 250, 190, 70, "Session service", ["jobs · SSE progress"], "ext")
    d.box("appr", 270, 370, 190, 70, "Approval service", ["HITL · intent binding"], "sec")
    d.box("admin", 270, 490, 190, 70, "Admin console", ["MCP · memory · rubrics"], "ext")
    d.group(505, 90, 560, 250, "① Agent runtime (stateless workers)", "agent")
    d.box("plan", 525, 130, 250, 80, "Planner", ["QuerySpec · frozen time window"], "agent")
    d.box("queue", 795, 130, 250, 80, "Job queue", ["job_steps · SKIP LOCKED", "checkpoints · resume"], "agent")
    d.box("orch", 525, 235, 250, 85, "Orchestrator loop", ["plan · call tools · draft answer"], "agent")
    d.box("sub", 795, 235, 250, 85, "Sub-agent runner", ["map · second opinion · verifier", "explorer · adjudicator"], "agent")
    d.group(505, 365, 560, 285, "② Context engine (custom)", "context")
    d.box("epoch", 525, 405, 160, 70, "Epoch manager", ["new conversation", "on budget / reset"], "context", size=13)
    d.box("coll", 700, 405, 160, 70, "Block collectors", ["15 block types"], "context", size=13)
    d.box("budget", 875, 405, 170, 70, "Budgeter / selector", ["priority · token caps"], "context", size=13)
    d.box("comp", 525, 495, 160, 70, "Compressor", ["custom summaries", "structured handoff"], "context", size=13)
    d.box("asm", 700, 495, 160, 70, "Payload assembler", ["order · fence · cache"], "context", size=13)
    d.box("snap", 875, 495, 170, 70, "Snapshotter", ["manifest + blobs", "replayable"], "context", size=13)
    d.box("meter", 525, 582, 160, 50, "Token meter", tone="context", size=13)
    d.group(1090, 90, 300, 330, "③ Memory service", "memory")
    d.box("stm", 1110, 130, 260, 90, "Short-term memory", ["session state · referents", "scratchpad · task ledger"], "memory")
    d.box("ltm", 1110, 235, 260, 90, "Long-term memory", ["semantic · episodic", "procedural · preferences"], "memory")
    d.box("prom", 1110, 340, 260, 60, "Promotion & review", ["provenance · conflicts"], "memory", size=13)
    d.group(1090, 440, 300, 210, "④ Data plane", "data")
    d.box("an", 1110, 480, 125, 70, "Analyzer", ["facts by SQL"], "data", size=13)
    d.box("rs", 1245, 480, 125, 70, "Record store", ["result sets"], "data", size=13)
    d.box("ver", 1110, 565, 125, 70, "Verifier engine", ["D-checks"], "data", size=13)
    d.box("ren", 1245, 565, 125, 70, "Renderer", ["lists · exports"], "data", size=13)
    d.group(1415, 90, 320, 560, "⑤ Tool layer", "tool")
    d.box("router", 1435, 130, 280, 66, "Tool router", ["selects tools per epoch"], "tool")
    d.box("tgw", 1435, 210, 280, 96, "Tool gateway", ["policy · credentials · rate limits", "offload · sanitise · audit"], "tool")
    names = ["jira", "zendesk", "sharepoint", "servicenow", "confluence", "postgres", "genie", "azure_monitor", "teams", "analyzer"]
    for i, n in enumerate(names):
        d.box(f"m{i}", 1435 + (i % 2) * 145, 322 + (i // 2) * 52, 135, 40, n, tone="tool", size=12)
    d.box("onb", 1435, 590, 280, 46, "⑥ Onboarding + probe sandbox", tone="tool", size=12)
    d.group(505, 675, 560, 110, "⑦ LLM gateway", "agent")
    d.box("llm", 525, 708, 520, 62, "Model routing · retries · refusal fallback · fairness · cost", tone="agent", size=13)
    d.group(25, 820, 1710, 200, "Storage & platform services", "data")
    d.box("pg", 60, 860, 300, 130, "PostgreSQL 16 + pgvector", ["events · jobs · records · facts", "memory · registry · audit chain"], "data", shape="cyl")
    d.box("blob", 400, 860, 220, 130, "Blob store", ["payload snapshots", "large results · exports"], "data", shape="cyl")
    d.box("vault", 660, 860, 200, 130, "Vault", ["OAuth apps · tokens"], "sec", shape="cyl")
    d.box("otel", 900, 870, 240, 110, "Telemetry", ["OpenTelemetry traces", "metrics · dashboards"], "ext")
    d.box("claude", 1200, 870, 230, 110, "Claude Messages API", ["inference only"], "ext")
    d.box("src", 1470, 870, 240, 110, "Source systems", ["Jira · Zendesk · SNOW", "Graph · Genie · Azure"], "ext")
    B = "#0284C7"
    d.line([(205, 165), (270, 165)], color=B)
    for i in (1, 2, 3):
        d.line([(205, 165 + i * 120), (238, 165 + i * 120)], color=B, width=1.4) if False else None
    d.back.append(f'<path d="M205,285 L238,285 M205,405 L238,405 M205,525 L238,525 M238,285 L238,525" fill="none" stroke="{B}" stroke-width="1.6"/>')
    d.line([(238, 285), (270, 285)], color=B)
    d.arrow("gw", "sess", fs="b", ts="t", color="#64748B")
    d.arrow("sess", "orch", fs="r", ts="l", via=[(492, 285), (492, 278)], label="enqueue", color="#64748B", lseg=0, ldy=-8)
    d.line([(525, 300), (492, 300), (492, 405), (460, 405)], color="#DC2626")
    d.text(365, 459, "▲ propose write (from orchestrator)", 10.5, "middle", 700, "#DC2626")
    d.arrow("orch", "coll", fs="b", ts="t", fo=0.75, to=0.4, label="build payload", color="#7C3AED", lseg=1)
    d.line([(650, 235), (650, 222), (1080, 222), (1080, 78), (1575, 78), (1575, 130)], "tool calls", color="#D97706", lseg=3)
    d.arrow("router", "tgw", fs="b", ts="t", color="#D97706")
    d.line([(1435, 280), (1402, 280), (1402, 515), (1370, 515)], "offload", color="#059669", lseg=1, ldx=0)
    d.line([(1045, 440), (1077, 440), (1077, 175), (1110, 175)], "read/write", color="#C026D3", lseg=1)
    d.arrow("asm", "llm", fs="b", ts="t", fo=0.5, to=0.36, color="#4F46E5")
    d.line([(785, 770), (785, 805), (1315, 805), (1315, 870)], color="#4F46E5")
    d.line([(1045, 530), (1070, 530), (1070, 812), (510, 812), (510, 860)], color="#7C3AED", dashed=True)
    d.line([(1715, 282), (1745, 282), (1745, 850), (1590, 850), (1590, 870)], color="#D97706")
    d.text(1745, 1050, "Credentials (Vault) are injected inside the tool gateway · every subsystem persists to PostgreSQL (arrows omitted)", 12, "end", 500, "#475569", True)
    d.text(25, 1050, "solid = request path · dashed = snapshot storage", 12, "start", 500, "#475569", True)
    return d

# ---------------------------------------------------------------- 03
@diagram
def arch_03_request_lifecycle():
    s = Sequence("Request lifecycle: a bulk question, then an approved write",
                 [("u", "User", "user"), ("o", "Orchestrator", "agent"), ("c", "Context engine", "context"),
                  ("l", "LLM gateway", "agent"), ("g", "Tool gateway", "tool"), ("r", "Record store", "data"),
                  ("w", "Map workers", "agent"), ("v", "Verifier", "agent"), ("a", "Approval svc", "sec")],
                 subtitle="“Network changes in the last 24h without CAB approval” · every arrow is also an event in session_events",
                 col=168, step=38)
    s.phase("1  PLAN")
    s.msg("u", "o", "question (+ user tz)")
    s.msg("o", "c", "build payload (epoch 1)")
    s.msg("c", "l", "request: system · profile · ledger · tools")
    s.msg("l", "o", "plan_query_spec → absolute UTC window", ret=True)
    s.phase("2  FETCH  (data never enters context)")
    s.msg("o", "g", "jira_…_to_resultset / servicenow_…")
    s.msg("g", "r", "paginate → 612 + 388 records")
    s.msg("g", "o", "handles + completeness proof + sample", ret=True)
    s.phase("3  MAP  (isolated sub-agents, batches of 25)")
    s.msg("o", "w", "analyzer_classify(handle, rubric)")
    s.msg("w", "r", "labels · coverage 100% · grounded")
    s.note("w", "r", "validators V1–V5 · recall net · second opinion")
    s.phase("4  REDUCE + DRAFT")
    s.msg("o", "g", "analyzer_aggregate → facts F-1, F-2")
    s.msg("o", "c", "build payload (facts, lists, scratchpad)")
    s.msg("l", "o", "draft: narrative + structured claims", ret=True)
    s.phase("5  VERIFY  (triple check)")
    s.msg("o", "v", "claims + facts + evidence only")
    s.msg("v", "o", "verdicts: all supported", ret=True)
    s.msg("o", "u", "answer + citations + completeness banner", ret=True)
    s.phase("6  ACT  (human-in-the-loop)")
    s.msg("u", "o", "“comment on the 14 and notify owners”")
    s.msg("o", "a", "propose 15 actions (frozen args, preview)")
    s.msg("a", "u", "approval card", ret=True)
    s.msg("u", "a", "approve")
    s.msg("a", "g", "execute once (idempotency keys)")
    s.msg("g", "o", "results + post-condition check", ret=True)
    return s.build()


# ---------------------------------------------------------------- context
@diagram
def ctx_01_epochs_and_layers():
    d = Diagram(1640, 940, "Context engine: epochs and payload layers",
                "A job runs as one or more epochs (model conversations). Each epoch's payload is assembled from budgeted layers.")
    # left: epoch timeline
    d.group(25, 90, 720, 810, "One job, several epochs", "context")
    d.box("e1", 55, 140, 300, 220, "Epoch 1", ["append-only conversation", "tool set fixed for the epoch", "", "turns: plan → fetch → map", "context grows ↑"], "context", size=16)
    d.box("e2", 415, 140, 300, 220, "Epoch 2", ["fresh conversation", "L0–L7 rebuilt from stores", "+ structured handoff", "", "turns: reduce → draft"], "context", size=16)
    d.arrow("e1", "e2", "reset", color="#7C3AED")
    d.text(385, 400, "An epoch ends when any trigger fires:", 13, "middle", 700, "#2E1065")
    trig = [("Budget", "working set > 55k tokens"), ("Tool set change", "model asked request_tools()"),
            ("Phase boundary", "ledger step kind changes"), ("Turn cap", "> 40 model turns"),
            ("Profile/playbook update", "new version approved"), ("Model switch", "fallback / routing change")]
    for i, (t, l) in enumerate(trig):
        d.box(f"t{i}", 55 + (i % 2) * 340, 420 + (i // 2) * 78, 320, 62, t, [l], "plain", size=13)
    d.box("hand", 55, 666, 660, 84, "Structured handoff (custom compressor, JSON)",
          ["goal · window · acceptance contract · ledger · facts · handles", "scratchpad (verbatim) · open questions · last user turn"], "memory", size=14)
    d.text(385, 790, "Why epochs: current models reject edited history (thinking-block check),", 12, "middle", 500, "#475569", True)
    d.text(385, 808, "so we never trim a live conversation. We start a new one from durable state.", 12, "middle", 500, "#475569", True)
    # right: stacked layers
    d.group(775, 90, 840, 810, "Payload of every model call (orchestrator, ≤ 120k tokens)", "context")
    layers = [("L0  Tool definitions", "router-selected subset", "≤ 8k", "cached", "tool"),
              ("L1  System core", "role · grounding & approval rules", "≤ 4k", "cached", "agent"),
              ("L2  Org profile", "approved semantic memory (pinned)", "≤ 3k", "cached", "memory"),
              ("L3  Playbook", "one selected procedure", "≤ 6k", "cached", "memory"),
              ("L4  Session state", "conversation summary · referents", "≤ 3k", "per epoch", "memory"),
              ("L5  Task ledger", "plan · steps · handles · facts", "≤ 3k", "per epoch", "agent"),
              ("L6  Scratchpad", "findings · hypotheses · open questions", "≤ 3k", "per epoch", "memory"),
              ("L7  Retrieved memory", "top-k long-term items with provenance", "≤ 3k", "per epoch", "memory"),
              ("L8  Working turns", "this epoch's turns, handles, samples", "≤ 55k", "grows", "context"),
              ("L9  Current user turn", "latest message / handoff", "≤ 2k", "volatile", "user"),
              ("R   Output reserve", "thinking + answer headroom", "≥ 30k", "—", "plain")]
    y = 130
    for i, (t, l, b, c, tone) in enumerate(layers):
        h = 120 if t.startswith("L8") else 52
        d.box(f"l{i}", 800, y, 560, h, t, [l], tone, size=13)
        d.text(1380, y + h / 2 + 4, b, 13, "start", 800, "#0F172A")
        d.text(1450, y + h / 2 + 4, c, 12, "start", 600, "#7C3AED" if c == "cached" else "#64748B")
        y += h + 8
    d.line([(1530, 136), (1530, 384)], None, color="#7C3AED")
    d.text(1540, 264, "stable", 11, "start", 700, "#7C3AED"); d.text(1540, 278, "prefix", 11, "start", 700, "#7C3AED")
    d.text(1195, 885, "Order = most stable first → prompt-cache hits; volatile content last", 12, "middle", 600, "#475569", True)
    return d

@diagram
def ctx_02_payload_pipeline():
    d = Diagram(1760, 860, "Payload builder pipeline and storage",
                "How every model request is assembled, recorded and made replayable (custom; no server-side context features)")
    steps = [("1 Collect", ["15 block collectors", "read stores, not the", "previous payload"]),
             ("2 Score", ["priority · recency", "relevance to goal", "mandatory flags"]),
             ("3 Fit budget", ["per-layer caps", "drop / degrade", "lowest score first"]),
             ("4 Compress", ["only if still over:", "custom summary", "of oldest blocks"]),
             ("5 Render", ["XML-tagged sections", "untrusted fencing", "canonical JSON"]),
             ("6 Order + cache", ["stable → volatile", "≤ 4 breakpoints", "deterministic"]),
             ("7 Count", ["token meter", "hard limit check", "fail closed"]),
             ("8 Snapshot", ["manifest of block", "hashes → DB", "content → blobs"]),
             ("9 Send", ["LLM gateway", "stream response", "log usage"])]
    for i, (t, ls) in enumerate(steps):
        d.box(f"s{i}", 30 + i * 183, 110, 165, 120, t, ls, "context" if i < 7 else "agent", size=14)
        if i:
            d.arrow(f"s{i-1}", f"s{i}", color="#7C3AED")
    d.group(30, 280, 1620, 250, "Sources the collectors read (block types)", "memory")
    srcs = [("System core", "prompts repo (versioned)"), ("Org profile", "org_profiles vN"), ("Playbook", "playbooks"),
            ("Tool defs", "tool router"), ("Session state", "session_state"), ("Referents", "session_referents"),
            ("Ledger", "jobs.ledger · job_steps"), ("Scratchpad", "scratchpad_entries"), ("Retrieved LTM", "memory_items"),
            ("Handles", "result_sets"), ("Facts", "facts"), ("Samples", "records (fenced)"),
            ("Epoch turns", "session_events (epoch)"), ("Handoff", "epochs.handoff"), ("User turn", "session_events")]
    for i, (t, l) in enumerate(srcs):
        d.box(f"b{i}", 50 + (i % 8) * 200, 320 + (i // 8) * 100, 190, 80, t, [l], "memory", size=13)
    d.line([(112, 280), (112, 230)], None, color="#C026D3")
    d.group(30, 570, 1620, 250, "Storage written by the pipeline", "data")
    d.box("ev", 60, 620, 330, 150, "session_events", ["append-only log of every", "request, response, tool call", "and result (source of truth)"], "data", shape="cyl")
    d.box("ep", 430, 620, 330, 150, "epochs", ["one row per model conversation", "trigger · handoff JSON", "token totals"], "data", shape="cyl")
    d.box("sn", 800, 620, 330, 150, "context_snapshots", ["manifest: block ids, hashes,", "layer, tokens, cache marks", "model params"], "data", shape="cyl")
    d.box("cb", 1170, 620, 450, 150, "content_blobs + blob store", ["content-addressed (sha256)", "rendered blocks, large tool results,", "dedup across epochs and jobs"], "data", shape="cyl")
    d.line([(1393, 230), (1393, 252), (1705, 252), (1705, 598), (965, 598), (965, 620)], "manifest + blobs", color="#4F46E5", lseg=3, lpos=0.3)
    d.line([(1395, 598), (1395, 620)], None, color="#4F46E5")
    d.line([(1576, 230), (1576, 242), (1735, 242), (1735, 560), (225, 560), (225, 620)], "request + response events", color="#4F46E5", lseg=3, lpos=0.4)
    d.text(840, 845, "Replay = load snapshot manifest → fetch blobs by hash → byte-identical request. Used for debugging, audits and eval regression.",
           12, "middle", 600, "#475569", True)
    return d

# ---------------------------------------------------------------- memory
@diagram
def mem_01_tiers():
    d = Diagram(1640, 900, "Memory: short-term, scratchpad, long-term",
                "Short-term memory lives for a session or job; long-term memory is curated, versioned and reviewed")
    d.group(25, 90, 760, 700, "Short-term memory (session / job scope)", "memory")
    stm = [("Working context", "the live epoch payload (L0–L9)", "one epoch", "context"),
           ("Session state", "rolling conversation summary · user tz · active goal", "session", "memory"),
           ("Referents", "“those tickets”, “the second one” → handles / ids", "session", "memory"),
           ("Task ledger", "plan, steps, status, handles, facts (harness-owned)", "job", "agent"),
           ("Scratchpad", "typed notes the agent writes while working", "job", "memory"),
           ("Result sets", "fetched records + labels (data plane, 30 days)", "job", "data")]
    for i, (t, l, sc, tone) in enumerate(stm):
        d.box(f"s{i}", 50, 135 + i * 105, 560, 85, t, [l], tone, size=15)
        d.badge(630, 135 + i * 105 + 47, sc, "memory" if sc != "one epoch" else "context")
    d.group(855, 90, 760, 700, "Long-term memory (org / user scope)", "memory")
    ltm = [("Semantic", "org facts: project keys, CI taxonomy, team map", "human review"),
           ("Episodic", "summaries of past verified jobs and findings", "auto, decays"),
           ("Procedural", "playbooks + rubrics (versioned procedures)", "approver"),
           ("Preferences", "per-user output and grouping preferences", "user confirms")]
    for i, (t, l, g) in enumerate(ltm):
        d.box(f"l{i}", 880, 135 + i * 125, 520, 100, t, [l], "memory", size=16)
        d.badge(1420, 135 + i * 125 + 55, g, "sec" if g in ("human review", "approver") else "memory")
    d.box("gate", 880, 650, 710, 110, "Promotion gate", ["provenance required · sensitivity check · dedupe / conflict detection", "no memory from untrusted ticket text alone · versioned, never overwritten"], "sec", size=15)
    d.line([(610, 572), (830, 572), (830, 705), (880, 705)], "promote (proposal)", color="#C026D3", lseg=0)
    d.line([(1235, 650), (1235, 610)], None, color="#C026D3")
    d.line([(880, 180), (700, 180), (700, 30 + 150)], None, color="#7C3AED", dashed=True) if False else None
    d.line([(880, 185), (840, 185), (840, 158), (610, 158)], "retrieve into L2 / L7", color="#7C3AED", dashed=True, lseg=2)
    d.text(820, 830, "Scratchpad ≠ memory: it is discarded (archived) at job end. Only reviewed, provenance-backed items reach long-term memory.",
           13, "middle", 700, "#4A044E")
    d.text(820, 856, "Scratchpad entries survive epoch resets (they are re-injected as L6), which is why the agent needs one.", 12, "middle", 500, "#475569", True)
    return d

@diagram
def mem_02_scratchpad():
    d = Diagram(1500, 760, "Scratchpad: lifecycle and rules",
                "A typed, bounded, job-scoped notebook. It bridges epoch resets without becoming a second memory.")
    d.box("start", 40, 120, 220, 80, "Job starts", ["empty scratchpad", "(per job, per sub-agent)"], "agent")
    d.box("write", 320, 120, 260, 80, "scratchpad_write", ["type · text ≤ 300 chars", "refs (required for findings)"], "memory")
    d.box("store", 640, 120, 240, 80, "scratchpad_entries", ["append-only rows", "supersede, never edit"], "data", shape="cyl")
    d.box("inject", 940, 120, 240, 80, "Injected as L6", ["every epoch", "newest first, ≤ 3k tokens"], "context")
    d.box("end", 1240, 120, 220, 80, "Job ends", ["archived with job", "read-only"], "agent")
    for a, b in [("start", "write"), ("write", "store"), ("store", "inject"), ("inject", "end")]:
        d.arrow(a, b, color="#C026D3")
    d.group(40, 250, 700, 470, "Entry types", "memory")
    types = [("finding", "verified fact with refs (record / fact ids)", "may be cited", "ok"),
             ("hypothesis", "unverified idea to test", "never in answers", "warn"),
             ("open_question", "something to ask the user or check", "drives next step", "plain"),
             ("decision", "choice made + reason (e.g. which field = CAB)", "audit trail", "plain"),
             ("todo", "follow-up the ledger does not cover", "short-lived", "plain"),
             ("warning", "data caveat: partial source, permission gap", "must reach answer", "sec")]
    for i, (t, l, r, tone) in enumerate(types):
        d.box(f"ty{i}", 60, 290 + i * 70, 420, 58, t, [l], tone, size=13)
        d.text(500, 290 + i * 70 + 34, r, 12.5, "start", 700, "#334155")
    d.group(770, 250, 690, 470, "Rules enforced by the harness", "sec")
    rules = ["Bounded: ≤ 40 active entries / 3k tokens; overflow → compressor merges oldest",
             "Findings must cite refs that exist in this job (validated on write)",
             "Hypotheses can never be used as claims; the verifier rejects them",
             "Warnings are copied into the answer's caveats automatically",
             "Sub-agents get their own scratchpad; only their summary returns",
             "Never auto-promoted: promotion goes through the memory gate",
             "Untrusted text may not be copied in verbatim (quotes ≤ 120 chars, fenced)"]
    for i, r in enumerate(rules):
        d.text(795, 300 + i * 58, f"{i+1}.", 14, "start", 800, "#DC2626")
        d.text(820, 300 + i * 58, r, 13, "start", 500, "#0F172A")
    return d

# ---------------------------------------------------------------- bulk + accuracy
@diagram
def bulk_01_pipeline():
    d = Diagram(1700, 720, "Bulk result pipeline (100–2,000 records)",
                "Records stay in the data plane. The model sees handles, facts and samples, never the 2,000 rows.")
    row1 = [("QuerySpec", ["absolute UTC window", "fields · sources"], "agent"),
            ("Fetch", ["connector-owned paging", "checkpoint per page"], "tool"),
            ("Completeness", ["exhaustion · count match", "bound check"], "sec"),
            ("Normalise", ["canonical records", "content hash"], "data"),
            ("Pre-filter", ["structured criteria in SQL", "in / out / judge"], "data")]
    for i, (t, l, tone) in enumerate(row1):
        d.box(f"a{i}", 40 + i * 330, 120, 280, 100, t, l, tone, size=16)
        if i:
            d.arrow(f"a{i-1}", f"a{i}", color="#059669")
    row2 = [("Map (batches of 25)", ["structured output", "short local ids"], "agent"),
            ("Validators V1–V5", ["coverage · grounding", "stop reason · retry cap"], "sec"),
            ("Recall net + 2nd opinion", ["negatives with signals", "low/medium + 5% sample"], "agent"),
            ("Reduce → facts", ["Analyzer SQL", "immutable, reproducible"], "data"),
            ("Render + verify", ["lists from DB", "triple check"], "ok")]
    for i, (t, l, tone) in enumerate(row2):
        d.box(f"b{i}", 40 + i * 330, 330, 280, 100, t, l, tone, size=16)
        if i:
            d.arrow(f"b{i-1}", f"b{i}", color="#059669")
    d.line([(1460, 220), (1460, 275), (180, 275), (180, 330)], "needs judgement", color="#059669", lseg=1)
    d.line([(1600, 220), (1600, 300), (1170, 300), (1170, 330)], None, color="#059669", dashed=True)
    d.text(1610, 262, "decided by", 11, "start", 600, "#059669"); d.text(1610, 276, "fields", 11, "start", 600, "#059669")
    d.line([(580, 430), (580, 470), (180, 470), (180, 430)], "retry failed rids only (smaller batch)", color="#DC2626", lseg=1)
    d.line([(700, 220), (700, 250), (870, 250)], None, color="#DC2626") if False else None
    d.group(40, 520, 1620, 170, "Guarantees", "ok")
    g = [("Exact counts", "numbers only from SQL facts"), ("100 % coverage", "every record labelled or flagged"),
         ("No invented records", "ids validated, lists rendered"), ("Honest completeness", "proven / bounded / incomplete"),
         ("Flat context", "orchestrator tokens independent of N")]
    for i, (t, l) in enumerate(g):
        d.box(f"g{i}", 60 + i * 320, 570, 300, 90, t, [l], "ok", size=15)
    return d

@diagram
def acc_01_triple_check():
    d = Diagram(1600, 760, "Accuracy: the triple check",
                "Three independent mechanisms; the database refuses to mark an answer verified unless all pass")
    d.box("draft", 40, 300, 220, 110, "Draft answer", ["narrative with", "placeholders + claims"], "agent", size=16)
    cols = [("Check 1 · Deterministic (code)", "data", ["D1 schema valid", "D2 every claim has refs", "D3 numbers == fact values",
                                                        "D4 ids exist in result sets", "D5 facts re-run identically", "D6–D8 caveats, window, coverage"]),
            ("Check 2 · Independent verifier (model)", "agent", ["sees claims, facts, evidence only", "A1 entailment per claim", "A2 scope (question + window)",
                                                                 "A3 absence needs typed null", "A4 causal claims need links", "A5 recommendations marked"]),
            ("Check 3 · Reconciliation & sampling", "ok", ["R1 cross-source reconciliation", "R2 2nd-opinion disagreement rate", "R3 daily human audit sample",
                                                         "R4 online LLM judge (10 %)", "R5 user feedback → eval cases", ""])]
    for i, (t, tone, ls) in enumerate(cols):
        x = 320 + i * 400
        d.group(x, 100, 370, 440, t, tone)
        for j, l in enumerate([l for l in ls if l]):
            d.box(f"c{i}{j}", x + 20, 145 + j * 62, 330, 50, l, tone="plain", bold=False, size=13)
    d.arrow("draft", "c00", fs="r", ts="l", via=[(290, 355), (290, 170)], color="#334155")
    d.line([(690, 320), (720, 320)], "pass", color="#16A34A")
    d.line([(1090, 320), (1120, 320)], "pass", color="#16A34A")
    d.box("out", 1150, 580, 410, 110, "Answer + confidence banner", ["Verified · Verified with caveats · Partially verified"], "ok", size=15)
    d.box("fix", 320, 580, 500, 110, "Revision (max 2)", ["specific feedback, e.g. “claim c2 says 15; F-2 = 14”", "after 2 failures → downgraded answer, flagged"], "warn", size=15)
    d.line([(505, 540), (505, 580)], "fail", color="#DC2626")
    d.line([(905, 540), (905, 635), (820, 635)], "fail", color="#DC2626")
    d.line([(1305, 540), (1305, 580)], "pass", color="#16A34A")
    d.line([(320, 635), (150, 635), (150, 410)], "re-draft", color="#EA580C", lseg=0)
    return d

# ---------------------------------------------------------------- tools
@diagram
def tool_01_router_gateway():
    d = Diagram(1700, 900, "Tool routing and the tool-gateway call path",
                "Custom tool router picks the tools for an epoch; every call passes the same gateway chain, whatever the server")
    d.group(25, 90, 520, 760, "Tool router (per epoch)", "tool")
    rs = [("Tool catalog", ["mcp_tools: enabled, approved hash"]), ("Role filter", ["allowed_roles × agent role"]),
          ("Relevance ranking", ["BM25 + embedding vs goal / playbook"]), ("Budget fit", ["≤ 8k tokens of definitions", "core tools always included"]),
          ("Epoch tool set (frozen)", ["+ request_tools(capability) meta-tool"])]
    for i, (t, l) in enumerate(rs):
        d.box(f"r{i}", 55, 140 + i * 120, 460, 90, t, l, "tool", size=15)
        if i:
            d.arrow(f"r{i-1}", f"r{i}", fs="b", ts="t", color="#D97706")
    d.text(285, 780, "request_tools → new epoch with the extra tools", 12, "middle", 600, "#7C3AED", True)
    d.text(285, 800, "(tools never change inside a live conversation)", 12, "middle", 500, "#475569", True)
    d.group(575, 90, 1100, 760, "Tool gateway chain (every call)", "tool")
    chain = [("1 Schema validate", "JSON Schema · actionable error", "tool"), ("2 Policy + RBAC", "tool × agent role × user role", "sec"),
             ("3 Write?", "→ create action, return pending_approval", "sec"), ("4 Rate limit", "per source token bucket · fairness", "tool"),
             ("5 Credentials", "user-delegated token from Vault", "sec"), ("6 Custom MCP server", "calls vendor REST API", "tool")]
    for i, (t, l, tone) in enumerate(chain):
        d.box(f"g{i}", 605, 140 + i * 112, 470, 84, t, [l], tone, size=15)
        if i:
            d.arrow(f"g{i-1}", f"g{i}", fs="b", ts="t", color="#D97706")
    back = [("7 Size check", "rows > 20 or > 4k tokens → offload", "data"), ("8 Sanitise + scan", "strip controls · injection flag", "sec"),
            ("9 Fence + shape", "handle summary or fenced result", "tool"), ("10 Audit + telemetry", "tool_calls · trace span", "ext")]
    for i, (t, l, tone) in enumerate(back):
        d.box(f"h{i}", 1150, 588 - i * 112, 490, 84, t, [l], tone, size=15)
        if i:
            d.arrow(f"h{i-1}", f"h{i}", fs="t", ts="b", color="#059669")
    d.arrow("g5", "h0", fs="r", ts="l", to=0.5, color="#059669", label="response")
    d.box("rs", 1150, 700, 490, 70, "Record store", ["full result kept here"], "data", shape="cyl")
    d.line([(1395, 672), (1395, 700)], None, color="#059669")
    d.line([(1395, 140), (1395, 112), (840, 112), (840, 140)], "result to model", color="#059669", lseg=1) if False else None
    d.line([(1395, 252), (1395, 150)], None, color="#059669")
    d.text(1395, 140, "result to the model (bounded, fenced)", 12, "middle", 700, "#059669")
    d.line([(1075, 350), (1110, 350), (1110, 800), (840, 800)], None, color="#DC2626", dashed=True) if False else None
    return d

@diagram
def mcp_01_onboarding():
    d = Diagram(1600, 700, "MCP server onboarding lifecycle",
                "Everything is automated except the decision: an admin approves before any tool reaches the agent")
    st = [("discovered", 40, 140, "plain"), ("probing", 300, 140, "tool"), ("pending_review", 560, 140, "warn"),
          ("canary", 860, 140, "agent"), ("enabled", 1160, 140, "ok"),
          ("intake_failed", 40, 330, "sec"), ("probe_failed", 300, 330, "sec"), ("rejected", 560, 330, "sec"),
          ("suspended", 1010, 330, "sec"), ("retired", 1310, 330, "plain")]
    for n, x, y, tone in st:
        d.box(n, x, y, 210, 70, n, tone=tone, shape="pill", size=15)
    d.arrow("discovered", "probing", "intake ok", color="#334155")
    d.arrow("probing", "pending_review", "report + score", color="#334155")
    d.arrow("pending_review", "canary", "admin approves", color="#16A34A")
    d.arrow("canary", "enabled", "eval gate", color="#16A34A")
    d.arrow("discovered", "intake_failed", fs="b", ts="t", color="#DC2626")
    d.arrow("probing", "probe_failed", fs="b", ts="t", color="#DC2626")
    d.arrow("pending_review", "rejected", fs="b", ts="t", color="#DC2626")
    d.arrow("enabled", "suspended", fs="b", ts="t", fo=0.3, to=0.7, label="drift / errors", color="#DC2626")
    d.arrow("canary", "suspended", fs="b", ts="t", fo=0.7, to=0.3, color="#DC2626")
    d.arrow("enabled", "retired", fs="b", ts="t", fo=0.8, to=0.3, color="#64748B")
    d.line([(1115, 400), (1115, 440), (815, 440), (815, 196), (772, 196)], "re-probe → review", color="#EA580C", lseg=1)
    d.group(40, 480, 1520, 190, "What each stage checks", "ext")
    cols = [("Discovery", ["internal registry mirror", "admin URL / GitOps", "capability-gap proposal"]),
            ("Intake", ["server.json valid", "publisher allow-list", "version + digest pinned"]),
            ("Probe (sandbox)", ["initialize · tools/list", "description injection scan", "write-verb detection"]),
            ("Review", ["read tools default", "tiers · roles · creds", "risk ≥ 60 → security"]),
            ("Watch", ["list_changed + daily", "schema-hash drift", "error / latency SLO"])]
    for i, (t, ls) in enumerate(cols):
        d.box(f"w{i}", 60 + i * 300, 520, 280, 130, t, ls, "plain", size=15)
    return d

# ---------------------------------------------------------------- data, security, ops
@diagram
def db_01_data_model():
    d = Diagram(1700, 960, "Data model by domain (PostgreSQL 16 + pgvector, schema itsm)",
                "Invariants enforced in the database: append-only logs, approval rules, verification gate, drift suspension")
    doms = [("Identity", "ext", ["orgs", "users", "roles", "user_roles"]),
            ("Sessions & jobs", "agent", ["sessions", "session_events ⟂", "jobs", "job_steps"]),
            ("Context engine", "context", ["epochs", "context_snapshots", "content_blobs"]),
            ("Short-term memory", "memory", ["session_state", "session_referents", "scratchpad_entries", "(jobs.ledger · job_steps)"]),
            ("Data plane", "data", ["query_specs", "result_sets", "result_set_pages", "records", "result_set_members",
                                    "rubrics", "map_batches", "record_annotations", "facts ⟂", "rendered_lists", "exports"]),
            ("Answers & checks", "ok", ["answers", "claims", "claim_evidence", "verifications"]),
            ("Actions & approvals", "sec", ["actions", "approvals ⟂"]),
            ("MCP registry", "tool", ["mcp_servers", "mcp_server_events", "mcp_probe_reports", "mcp_tools", "tool_policies"]),
            ("Long-term memory", "memory", ["memory_items (vector, tsv)", "memory_reads", "answer_memory_refs", "playbooks", "org_profiles"]),
            ("Telemetry & audit", "ext", ["llm_calls", "tool_calls", "audit_log ⟂ (hash chain)"]),
            ("Evaluation", "plain", ["eval_cases", "eval_runs", "eval_results"])]
    pos = [(30, 100), (30, 320), (30, 560), (30, 745), (450, 100), (870, 100), (870, 360), (1290, 100), (1290, 420), (870, 560), (870, 760)]
    for (t, tone, tabs), (x, y) in zip(doms, pos):
        tabs = [x for x in tabs if x]
        h = 60 + 34 * len(tabs)
        d.group(x, y, 390, h, t, tone)
        for j, tb in enumerate(tabs):
            d.box(f"{t}{j}", x + 20, y + 42 + j * 34, 350, 28, tb, tone="plain", bold=False, size=12.5)
    d.text(1290 + 195, 800, "⟂ = append-only (trigger-enforced)", 13, "middle", 700, "#DC2626")
    return d

@diagram
def sec_01_approval_flow():
    d = Diagram(1640, 640, "Human-in-the-loop write flow",
                "No write reaches a source system without an approved, immutable, intent-bound action")
    st = [("Write tool call", ["from orchestrator only"], "agent"), ("Gateway policy", ["tier + escalations", "(batch > 10, public reply…)"], "sec"),
          ("Action created", ["frozen args · preview", "idempotency key · user turn"], "sec"), ("Approval card", ["console + Teams", "per-item exclude"], "user"),
          ("Decision", ["DB triggers enforce", "role · SoD · count · expiry"], "sec"), ("Executor", ["precondition re-check", "execute once"], "tool"),
          ("Post-condition", ["re-read target", "audit chain"], "ok")]
    for i, (t, l, tone) in enumerate(st):
        d.box(f"s{i}", 30 + i * 230, 130, 205, 110, t, l, tone, size=15)
        if i:
            d.arrow(f"s{i-1}", f"s{i}", color="#DC2626" if i in (2, 4) else "#334155")
    d.group(30, 300, 1580, 300, "Approval tiers", "sec")
    tiers = [("read", "no approval", "ok"), ("low", "auto + audit", "ok"), ("medium", "requester may approve (1)", "warn"),
             ("high", "1 approver, not requester", "sec"), ("critical", "2 distinct approvers", "sec")]
    for i, (t, l, tone) in enumerate(tiers):
        d.box(f"t{i}", 60 + i * 308, 350, 285, 90, t, [l], tone, size=17)
    d.text(820, 490, "Reject → action rejected · arguments immutable after proposal · expired actions cannot be approved", 13, "middle", 600, "#450A0A")
    d.text(820, 520, "Concurrent approvers are serialised by a row lock (tested: exactly one decision recorded)", 13, "middle", 500, "#475569", True)
    d.text(820, 560, "Injected instructions in ticket text can at most create a proposal that a human sees", 13, "middle", 700, "#DC2626")
    return d

@diagram
def ops_01_deployment():
    d = Diagram(1640, 880, "Reference deployment (Azure, single organisation)",
                "Self-hosted on AKS; swap each managed service for its on-prem / AWS equivalent without design changes")
    d.group(25, 90, 1100, 560, "AKS cluster · namespace itsm-agent", "agent")
    svc = [("session-service ×2", "SSE · REST", "ext"), ("harness-workers ×3–20", "HPA on queue depth", "agent"),
           ("llm-gateway ×2", "routing · fairness", "agent"), ("tool-gateway ×2", "policy · offload", "tool"),
           ("action-executor ×2", "write credentials only", "sec"), ("approval-service ×2", "cards · decisions", "sec"),
           ("mcp-servers (10)", "one deployment each", "tool"), ("registry-onboarding", "probe orchestration", "tool"),
           ("memory-service ×2", "retrieval · promotion", "memory")]
    for i, (t, l, tone) in enumerate(svc):
        d.box(f"k{i}", 55 + (i % 3) * 355, 140 + (i // 3) * 120, 330, 90, t, [l], tone, size=15)
    d.group(55, 510, 1040, 115, "Isolated node pool · probe sandbox (no egress by default)", "sec", dashed=True)
    d.box("sbx", 85, 545, 980, 60, "ephemeral containers for new MCP servers · no secrets · resource limits", tone="sec", size=13)
    d.group(1160, 90, 455, 560, "Managed services", "data")
    ms = [("Azure Database for PostgreSQL", "Flexible Server · pgvector · HA", "data"), ("Blob Storage", "snapshots · exports (lifecycle rules)", "data"),
          ("Key Vault", "OAuth apps · tokens", "sec"), ("Entra ID", "OIDC · on-behalf-of", "sec"), ("Azure Monitor / OTel", "traces · metrics · alerts", "ext")]
    for i, (t, l, tone) in enumerate(ms):
        d.box(f"m{i}", 1185, 135 + i * 100, 405, 80, t, [l], tone, size=15)
    d.group(25, 690, 1590, 160, "Egress allow-list (nothing else leaves the cluster)", "ext")
    eg = ["Claude API endpoint", "Atlassian Cloud", "Zendesk", "ServiceNow", "Microsoft Graph", "Databricks workspace", "Azure ARM / Log Analytics"]
    for i, t in enumerate(eg):
        d.box(f"e{i}", 45 + i * 223, 740, 208, 70, t, tone="ext", size=13)
    return d

if __name__ == "__main__":
    only = sys.argv[1:]
    for name, fn in REG.items():
        if only and name not in only:
            continue
        with open(os.path.join(OUT, name + ".svg"), "w") as f:
            f.write(fn().svg())
        print("wrote", name)
