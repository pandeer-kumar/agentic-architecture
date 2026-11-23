# 01 — Research Basis

The design is derived from published engineering guidance. Each finding below is mapped to the
concrete mechanism it drives in this architecture. Numbers are quoted from the sources; they are
the sources' own measurements, not ours.

## 1. Anthropic engineering posts

| Source | Key finding | Where it lands in this design |
|---|---|---|
| [Building effective agents](https://www.anthropic.com/engineering/building-effective-agents) | Start with the simplest pattern; workflows (chaining, routing, parallelisation, orchestrator–workers, evaluator–optimizer) before free-form agents. Invest in the agent–computer interface; "poka-yoke" tools. | Hybrid: a **deterministic pipeline** for bulk jobs (doc 05) wrapped by a model-directed orchestrator. Tools designed so mistakes are hard (absolute time windows, typed enums, handle IDs). |
| [Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) | Context is a finite "attention budget"; *context rot* — recall degrades as the window fills. Just-in-time retrieval via lightweight references; compaction; structured note-taking; sub-agents return condensed 1–2k-token summaries. | Doc 03: hard per-layer budgets, *handles not data*, task ledger notes, sub-agent isolation with bounded return size. |
| [Writing effective tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents) | Consolidate around workflows, namespace tools (`service_resource_verb`), return high-signal fields not UUIDs, offer `response_format` concise/detailed, paginate/truncate with sane defaults (Claude Code caps tool responses at 25k tokens), actionable error messages, evaluate tools with realistic tasks. | Doc 07 tool contracts: namespaced names, `response_format`, default limits, resolved human-readable fields, error messages with a fix. Gateway truncation cap (doc 03). |
| [How we built our multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system) | Orchestrator–worker with parallel sub-agents beat single-agent by 90.2 % on their eval; multi-agent uses ~15× chat tokens. Delegation needs objective, output format, tool guidance, boundaries. Explicit effort-scaling rules. Plan saved to memory. Dedicated citation agent. LLM-as-judge rubric (factual accuracy, citation accuracy, completeness, source quality, tool efficiency). Checkpoints + resume; rainbow deploys. | Doc 02 agent roles (Orchestrator, Map workers, Verifier, Citation check); doc 05 effort scaling table; doc 06 citation checking; doc 11 judge rubric; doc 12 checkpoint/resume. |
| [Code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp) | Loading every tool definition and routing intermediate results through the model is costly and error-prone (example: 150k → 2k tokens, 98.7 % reduction). Filter data in the execution environment; tokenise PII so it flows between tools without entering the model. | ADR-01 data plane: results land in the record store; the Analyzer runs deterministic queries; optional PII tokenisation in the gateway (doc 10). |
| [Introducing advanced tool use](https://www.anthropic.com/engineering/advanced-tool-use) | **Tool Search**: 58 tools ≈ 55k tokens; deferring reduced context ~85 % and raised MCP-eval accuracy (Opus 4.5: 79.5 % → 88.1 %). Use when definitions > ~10k tokens or 10+ tools. **Programmatic tool calling** −37 % tokens on complex tasks. **Tool use examples** raised complex-parameter accuracy 72 % → 90 %. | ADR-07 tool search with `defer_loading`; tool-use examples for JQL/Zendesk-query tools (doc 07); PTC is an optional optimisation (doc 05 §9). |
| [Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) | Each new session starts with no memory; use an initializer that writes a structured feature/task list (JSON preferred over Markdown because models overwrite it less), a progress log, and git checkpoints; one unit of work per session; verify end-to-end before marking done. | Task ledger as **structured JSON in Postgres** (`job_steps`), progress notes, resume-from-checkpoint, "only the status field is mutable by the model" (doc 03 §6, doc 09). |
| [Harness design for long-running application development](https://www.anthropic.com/engineering/harness-design-long-running-apps) | Planner / generator / evaluator split; "sprint contracts" agreed before work; out-of-the-box Claude is a lenient self-evaluator — a separately prompted, calibrated evaluator with hard thresholds works better. Every harness component encodes an assumption about model limits; re-test and remove components when models improve. | Doc 06 verifier is a separate agent with hard pass/fail thresholds and few-shot calibration; "acceptance contract" generated at plan time; doc 11 ablation testing of harness components. |
| [Scaling Managed Agents: decoupling the brain from the hands](https://www.anthropic.com/engineering/managed-agents) | Split into **session** (append-only durable event log), **harness** (stateless loop, restartable via `wake(sessionId)`), **sandbox** (cattle, not pets). Credentials never reachable from the sandbox; a proxy injects them. Keep durable storage separate from context management since compaction is irreversible. | ADR-05 append-only `session_events`; stateless harness workers resume from the log; credential vault + gateway injection (doc 10); context is a re-buildable view (doc 03). |

## 2. Harness-engineering writing (non-Anthropic)

| Source | Finding | Design consequence |
|---|---|---|
| OpenAI — *Harness engineering: leveraging Codex in an agent-first world* (Feb 2026) | The environment (docs, linters, tests, feedback loops) determines agent reliability as much as the model. | Treat the harness (validators, contracts, eval) as the product; doc 11 makes harness components testable. |
| LangChain — [Improving Deep Agents with harness engineering](https://langchain.com/blog/improving-deep-agents-with-harness-engineering) | Same model, harness-only changes moved Terminal-Bench 2.0 from 52.8 % to 66.5 % (as reported). | Harness changes are versioned and evaluated like model changes (doc 11 §6). |
| Addy Osmani — [Agent Harness Engineering](https://addyosmani.com/blog/agent-harness-engineering/) | Scaffolding is an artifact that tightens every time the agent slips. | Every production failure becomes a regression eval case (doc 11 §5). |

> Note: the harness-engineering figures in §2 were taken from search snippets and secondary write-ups;
> they motivate process, not any numeric target in this design.

## 3. Protocol and vendor facts the design depends on

| Fact | Source | Consequence |
|---|---|---|
| MCP `tools/list` is cursor-paginated; servers that declare `tools.listChanged` emit `notifications/tools/list_changed`; results may carry `structuredContent` validated against `outputSchema`; execution errors return `isError: true`; **annotations are untrusted unless the server is trusted** | MCP spec 2025-11-25, *Server › Tools* | Doc 08: re-list on change, diff and re-review; never trust a new server's `readOnlyHint` — the gateway assigns risk itself. |
| The official MCP Registry (`registry.modelcontextprotocol.io`) publishes `server.json` metadata; its API froze at v0.1 (Oct 2025); host apps are expected to consume via downstream/sub-registries | MCP Registry docs | Doc 08: discovery reads the official registry **through an internal mirror/sub-registry** with an allow-list. |
| Jira Cloud legacy `/rest/api/3/search` is removed; enhanced `/rest/api/3/search/jql` uses `nextPageToken` and returns **no total**; a separate `/search/approximate-count` exists | Atlassian KB & community | Doc 05 completeness proof uses page-exhaustion as truth and approximate-count only as a sanity bound. |
| Atlassian Rovo MCP server (`https://mcp.atlassian.com/v2/mcp`) exposes `searchJiraIssuesUsingJql`; a public issue reports pagination tokens not returned | Atlassian docs; [atlassian-mcp-server#118](https://github.com/atlassian/atlassian-mcp-server/issues/118) | Bulk retrieval uses our own connector tool (doc 07 §1); the Rovo server may be enabled for interactive use. |
| Zendesk Search API returns ≤ 1,000 results (≤ 100/page); `/api/v2/search/export` is cursor-based, one object type per request, cursor expires ~1 h, separate rate limit | Zendesk developer docs (via search) | Zendesk connector switches to export search whenever `count` > 1,000 or always for bulk mode (doc 07 §2). |
| Zendesk had **no generally available first-party MCP server** as of Oct 2026 (announced May 2026) | Vendor comparison write-ups | Zendesk MCP server is built in-house (doc 07 §2). Re-check before build. |
| Microsoft's original SharePoint/OneDrive MCP servers deprecated from 13 Mar 2026; new Microsoft SharePoint MCP server is preview; file ops ≤ 5 MB | Microsoft Learn (via search) | SharePoint server = in-house Graph-based server for production, official preview server optional (doc 07 §3). |
| Azure Databricks Genie managed MCP: `https://<workspace>/api/2.0/mcp/genie/{space_id}` (per-space) and a Genie One endpoint; Beta; Conversation API throughput is limited (best-effort, ~5 questions/min on the free tier; only POSTs count) | Azure Databricks docs (via search) | Genie calls are rate-limited at the gateway and treated as *analytical*, low-QPS tools (doc 07 §5). |
| Postgres MCP Pro offers a restricted (read-only transaction + time limit) mode | crystaldba/postgres-mcp | The Postgres server runs read-only against a replica with a read-only role (doc 07 §4). |

## 4. Claude API capabilities used (verified against the current API reference)

| Capability | Use here |
|---|---|
| Adaptive thinking + `output_config.effort` | Orchestrator `high`; map workers `low`/`medium`; verifier `high` |
| Structured outputs (`output_config.format`) and `strict: true` tools | Map-worker outputs and verifier verdicts are schema-guaranteed |
| Tool search (`tool_search_tool_bm25_20251119`, `defer_loading: true`) | Scale to many MCP servers without loading every schema |
| Context editing (`clear_tool_uses_20250919`, beta `context-management-2025-06-27`) | Clear stale tool results in long orchestrator loops |
| Server-side compaction (beta `compact-2026-01-12`) | Safety net for very long sessions (primary mechanism is our own ledger) |
| Prompt caching + mid-conversation `system` messages | Stable prefix (system + tools) cached; operator instructions appended without breaking cache |
| Message Batches API | Optional: asynchronous map phase for non-interactive jobs at lower cost |
| Citations on `document` blocks | SharePoint/Confluence document Q&A with char/page-level citations (citations can't be combined with structured outputs in the same request, so explorers use citations and return prose; the orchestrator's final draft uses structured outputs) |

> Platform caveat: several of these are beta and availability differs on Bedrock / Vertex / Foundry.
> Doc 02 §7 lists fallbacks if a feature is unavailable on the chosen platform.
