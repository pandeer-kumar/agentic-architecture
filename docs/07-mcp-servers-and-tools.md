# 07 — MCP Servers & Tools

Ten servers, 50 tools. The authoritative, machine-checked definitions (JSON Schema inputs, risk
tier, MCP annotations, allowed roles) are in [`contracts/tools/`](../contracts/tools/); the tables
below are generated from those files, so the doc and the contracts can't drift apart (checked in doc 13).

## 1. Conventions every tool follows

| Convention | Rule | Source |
|---|---|---|
| Naming | `<server>_<resource>_<verb>`, snake_case, unique across all servers | Writing-tools post: namespacing |
| Bulk tools | Suffix `_to_resultset`: paginate inside the connector, write to the record store, return a **handle summary** with completeness proof | ADR-01 |
| Inputs | Strict JSON Schema (`additionalProperties: false`), enums for closed sets, absolute UTC windows | Poka-yoke |
| Limits | `max_records` defaults to 2,000 and is enforced by the gateway (JSON-Schema numeric bounds aren't supported by strict mode, so bounds are checked in code) | Doc 05 §2 |
| `response_format` | `concise` (default) or `detailed` on single-record getters | Writing-tools post |
| Output | Human-readable resolved fields (names not opaque ids, plus the id for follow-ups); text from users wrapped as untrusted | Doc 06 §5 |
| Errors | `isError: true` + message stating the fix and a valid example | MCP spec, writing-tools post |
| Writes | Every write tool returns `pending_approval` first; execution happens only after approval, with an idempotency key | Doc 10 |
| Annotations | `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint` are declared, but **the gateway's own risk tier is authoritative** — annotations from a server are hints, not trust | MCP spec |

### Risk tiers (used by the policy engine, doc 10)

| Tier | Meaning | Default policy |
|---|---|---|
| `read` | No side effects | Allow (RBAC still applies) |
| `low` | Side effects only inside the platform (exports, classification jobs) | Allow + audit |
| `medium` | Reversible external write, internal audience (work note, internal comment, Teams post) | Requesting user approves (preview shown) |
| `high` | Customer-visible or state-changing (public reply, transition, update, create) | Approver role required |
| `critical` | Irreversible or bulk destructive (merge, bulk close) | Two approvers |

Escalation: a batch of more than 10 writes moves up one tier; any `zendesk_ticket_comment_add` with
`public=true` is `high`.

## 2. Server map

| # | Server | Role in the platform | Writes? | Official MCP available? |
|---|---|---|---|---|
| 1 | `jira` | Changes, incidents, problems (Jira / JSM) | yes | Atlassian Rovo MCP (remote) — used for interactive tools; bulk via in-house connector because of the pagination limits noted in doc 01 §3 |
| 2 | `zendesk` | Customer tickets | yes | No GA first-party server as of Oct 2026 → in-house |
| 3 | `sharepoint` | Runbooks, policies, change logs (lists) | yes | Microsoft SharePoint MCP (preview) — optional |
| 4 | `postgres` | Internal ops DBs / reporting replica | no | Community (Postgres MCP Pro, restricted mode) |
| 5 | `genie` | Analytics over the lakehouse in natural language | no | Azure Databricks managed MCP (Beta) |
| 6 | `analyzer` | Deterministic computation over the record store | platform-internal only | In-house (core of the data plane) |
| 7 | `confluence` | Knowledge base | yes | Atlassian Rovo MCP |
| 8 | `servicenow` | ITSM system of record: change, incident, CMDB, approvals | yes | In-house (vendor MCP optional) |
| 9 | `teams` | Notifications to allow-listed channels | yes | In-house (Graph) |
| 10 | `azure_monitor` | Ground truth of what actually changed in Azure networking | no | In-house |

Why `azure_monitor` matters for the headline use case: tickets record what people *said* they changed;
the Activity Log records what *was* changed. Joining them (Analyzer `join`) finds network changes
with no ticket, which is the strongest accuracy check for "all network changes in the last 24 h".

## 3. Tools by server

### 3.1 `jira`

- **Provenance:** in-house (Jira Cloud REST v3). Atlassian Rovo MCP optional for interactive use
- **Transport:** streamable-http · **Auth:** OAuth 2.0 (3LO) per user; service account for scheduled jobs
- **Rate limiting:** gateway token bucket, honour Retry-After on 429

| Tool | Risk | Bulk → record store | Key inputs | Purpose |
|---|---|---|---|---|
| `jira_issues_search_to_resultset` | read | yes | `jql`, `fields`, `window`, `max_records`, `query_spec_id` | Run a JQL search and store ALL matching issues in the record store. |
| `jira_issues_count_approx` | read |  | `jql` | Approximate count for a JQL query (Jira approximate-count). |
| `jira_issue_get` | read |  | `issue_key`, `response_format`, `include_comments` | Fetch one issue by key with resolved human-readable fields. |
| `jira_fields_list` | read |  | `project_key` | List field ids, names and types available in a project (use before writing JQL with custom fields). |
| `jira_issue_comment_add` | medium |  | `issue_key`, `body`, `visibility_group` | Propose adding a comment to an issue. |
| `jira_issue_link` | medium |  | `from_key`, `to_key`, `link_type` | Propose linking two issues with a link type name (e.g. 'Relates', 'Duplicate'). |
| `jira_issue_transition` | high |  | `issue_key`, `transition_name`, `comment` | Propose moving an issue through a workflow transition (e.g. close). |
| `jira_issue_create` | high |  | `project_key`, `issue_type`, `summary`, `description`, `labels` | Propose creating an issue. |

### 3.2 `zendesk`

- **Provenance:** in-house (Zendesk Support REST API); no GA first-party MCP server as of Oct 2026
- **Transport:** streamable-http · **Auth:** OAuth 2.0 per user; API token for service account
- **Rate limiting:** account limit per plan; search export 100 req/min; honour Retry-After

| Tool | Risk | Bulk → record store | Key inputs | Purpose |
|---|---|---|---|---|
| `zendesk_tickets_search_to_resultset` | read | yes | `query`, `window`, `max_records`, `query_spec_id` | Run a Zendesk search query and store ALL matching tickets. |
| `zendesk_tickets_count` | read |  | `query` | Exact result count for a search query (search count endpoint). |
| `zendesk_ticket_get` | read |  | `ticket_id`, `response_format`, `include_comments` | Fetch one ticket; detailed includes comments (public and internal flagged). |
| `zendesk_ticket_comment_add` | medium |  | `ticket_id`, `body`, `public` | Propose a comment. |
| `zendesk_ticket_update` | high |  | `ticket_id`, `status`, `priority`, `assignee_email`, `tags_add`, `tags_remove` | Propose updating status, priority, assignee or tags. |
| `zendesk_tickets_merge` | critical |  | `target_ticket_id`, `source_ticket_ids`, `target_comment`, `source_comment` | Propose merging source tickets into a target ticket. |

### 3.3 `sharepoint`

- **Provenance:** in-house (Microsoft Graph). Microsoft SharePoint MCP server (preview) optional
- **Transport:** streamable-http · **Auth:** Entra ID delegated (on-behalf-of) per user; Sites.Selected for service account
- **Rate limiting:** Graph throttling; honour Retry-After

| Tool | Risk | Bulk → record store | Key inputs | Purpose |
|---|---|---|---|---|
| `sharepoint_search` | read |  | `query`, `site_urls`, `file_types`, `limit` | Search documents and pages the user can access. |
| `sharepoint_document_get` | read |  | `drive_item_id`, `format`, `max_chars` | Fetch a document's text (or PDF) for citation-enabled reading. |
| `sharepoint_list_items_to_resultset` | read | yes | `site_url`, `list_name`, `odata_filter`, `window`, `max_records` | Store all items of a SharePoint list matching an OData filter in the record store. |
| `sharepoint_list_item_create` | medium |  | `site_url`, `list_name`, `fields_json` | Propose creating a list item (e.g. change log entry). |
| `sharepoint_file_upload` | medium |  | `site_url`, `folder_path`, `file_name`, `export_id` | Propose uploading a generated report (from analyzer_export) to a folder. |

### 3.4 `postgres`

- **Provenance:** Postgres MCP Pro (restricted mode) or in-house; read-only role on a replica
- **Transport:** streamable-http · **Auth:** DB role itsm_agent_ro via vault; statement_timeout 15s
- **Rate limiting:** max 4 concurrent queries per job

| Tool | Risk | Bulk → record store | Key inputs | Purpose |
|---|---|---|---|---|
| `postgres_schema_list` | read |  | `schema` | List schemas, tables and views the agent role can read, with row-count estimates. |
| `postgres_table_describe` | read |  | `table` | Columns, types, keys, comments and 3 sample rows of a table. |
| `postgres_query_to_resultset` | read | yes | `sql`, `max_rows` | Run a single read-only SELECT (inside a READ ONLY transaction with timeout) and store rows in the record store. |
| `postgres_query_explain` | read |  | `sql` | EXPLAIN (no ANALYZE) a SELECT to check cost before running it. |

### 3.5 `genie`

- **Provenance:** Azure Databricks managed MCP (Genie space endpoint, Beta). Gateway aliases native tool names
- **Transport:** streamable-http · **Auth:** Entra ID / Databricks OAuth (user-to-machine)
- **Rate limiting:** low QPS (Conversation API best-effort, ~5 questions/min on free tier); gateway queue + 409 handling

| Tool | Risk | Bulk → record store | Key inputs | Purpose |
|---|---|---|---|---|
| `genie_space_ask` | read |  | `space_id`, `question`, `conversation_id` | Ask a natural-language analytics question to an approved Genie space. |
| `genie_result_to_resultset` | read | yes | `space_id`, `conversation_id`, `message_id`, `attachment_id` | Store the full query result of a Genie message attachment in the record store. |

### 3.6 `analyzer`

- **Provenance:** in-house; deterministic computation over the record store
- **Transport:** in-process / streamable-http · **Auth:** platform internal (job-scoped token)
- **Rate limiting:** n/a

| Tool | Risk | Bulk → record store | Key inputs | Purpose |
|---|---|---|---|---|
| `analyzer_resultset_describe` | read |  | `handle` | Row count, columns, completeness proof and field value distributions of a handle. |
| `analyzer_records_sample` | read |  | `handle`, `n`, `filters`, `fields` | Return up to 25 records (fenced, sanitised) from a handle, optionally filtered. |
| `analyzer_resultset_filter` | read |  | `handle`, `filters` | Create a new handle with rows matching structured filters (field op value). |
| `analyzer_resultset_union` | read |  | `handles`, `dedupe_key` | Union handles and dedupe by a key expression (e.g. normalised change number). |
| `analyzer_resultset_join` | read |  | `left`, `right`, `left_key`, `right_key`, `mode` | Join two handles on keys; returns matched / left-only / right-only handles and counts. |
| `analyzer_classify` | low |  | `handle`, `rubric_id`, `only_undecided` | Run the batched map phase over a handle with a versioned rubric. |
| `analyzer_aggregate` | read |  | `handle`, `filters`, `group_by`, `metric`, `metric_field`, `label_filter` | Compute a fact: count/sum/min/max/count_distinct grouped by fields, over a handle and optional label filter. |
| `analyzer_list_create` | read |  | `handle`, `filters`, `columns`, `sort` | Define a rendered list (placeholder L-n) over a handle: filters, columns, sort. |
| `analyzer_export` | low |  | `source_id`, `format` | Export a handle or list to CSV or XLSX; returns a download link valid for 24h. |

### 3.7 `confluence`

- **Provenance:** Atlassian Rovo MCP (official, remote) behind the gateway, aliased; in-house fallback
- **Transport:** streamable-http · **Auth:** OAuth 2.1 per user
- **Rate limiting:** Atlassian limits; honour Retry-After

| Tool | Risk | Bulk → record store | Key inputs | Purpose |
|---|---|---|---|---|
| `confluence_pages_search` | read |  | `cql`, `limit` | Search Confluence with CQL; returns titles, spaces, URLs, excerpts. |
| `confluence_page_get` | read |  | `page_id`, `max_chars` | Fetch a page body as text for citation-enabled reading. |
| `confluence_page_comment_add` | medium |  | `page_id`, `body` | Propose a footer comment on a page. |

### 3.8 `servicenow`

- **Provenance:** in-house (Table API, CMDB API); vendor MCP optional
- **Transport:** streamable-http · **Auth:** OAuth 2.0 per user; integration user for scheduled jobs
- **Rate limiting:** instance rate-limit rules; honour 429

| Tool | Risk | Bulk → record store | Key inputs | Purpose |
|---|---|---|---|---|
| `servicenow_records_search_to_resultset` | read | yes | `table`, `encoded_query`, `fields`, `window`, `max_records`, `query_spec_id` | Store ALL records of a table matching an encoded query in the record store (offset pagination ordered by sys_id; X-Total-Count reconciled). |
| `servicenow_record_get` | read |  | `table`, `number_or_sys_id`, `response_format` | Fetch one record by number or sys_id with display values. |
| `servicenow_change_approvals_to_resultset` | read | yes | `change_handle` | Store approval records (sysapproval_approver) for all change requests in a handle. |
| `servicenow_ci_relations_get` | read |  | `ci_sys_id`, `depth` | Upstream/downstream CMDB relationships of a CI up to depth 3. |
| `servicenow_work_note_add` | medium |  | `table`, `sys_id`, `text` | Propose adding an internal work note. |
| `servicenow_record_update` | high |  | `table`, `sys_id`, `fields_json` | Propose updating fields (state, assignment_group, close_code ...). |
| `servicenow_incident_create` | high |  | `short_description`, `description`, `cmdb_ci`, `assignment_group`, `impact`, `urgency` | Propose creating an incident. |

### 3.9 `teams`

- **Provenance:** in-house (Microsoft Graph); destinations allow-listed
- **Transport:** streamable-http · **Auth:** Entra ID app (ChannelMessage.Send via RSC) 
- **Rate limiting:** Graph throttling

| Tool | Risk | Bulk → record store | Key inputs | Purpose |
|---|---|---|---|---|
| `teams_channels_list` | read |  | — | List allow-listed Teams channels the agent may post to. |
| `teams_user_lookup` | read |  | `query` | Resolve a person by e-mail or display name to a Teams user (for @mentions). |
| `teams_message_post` | medium |  | `channel_ref`, `text_md`, `mention_user_ids` | Propose posting a message (optionally with @mentions) to an allow-listed channel. |

### 3.10 `azure_monitor`

- **Provenance:** in-house (Azure Resource Manager Activity Log + Log Analytics query API)
- **Transport:** streamable-http · **Auth:** Managed identity with Reader + Log Analytics Reader
- **Rate limiting:** ARM / Log Analytics throttling

| Tool | Risk | Bulk → record store | Key inputs | Purpose |
|---|---|---|---|---|
| `azure_monitor_activity_log_to_resultset` | read | yes | `subscription_ids`, `resource_type_prefixes`, `window`, `max_records` | Store Azure Activity Log write/delete operations for resource types (e.g. Microsoft.Network/*) in the window. |
| `azure_monitor_log_analytics_query_to_resultset` | read | yes | `workspace_id`, `kql`, `window`, `max_records` | Run a KQL query against an approved workspace over the window and store rows. |
| `azure_monitor_resource_get` | read |  | `resource_id` | Get a resource's properties and tags by resource id. |

## 4. Example: handle summary returned by a bulk tool

```json
{
  "handle": "rs_01HZX3KA",
  "source": "servicenow",
  "row_count": 388,
  "completeness": {"status": "proven", "method": "count_match", "source_reported_total": 388, "pages": 1},
  "window_utc": ["2026-11-22T09:00:00Z", "2026-11-23T09:00:00Z"],
  "columns": ["number", "short_description", "category", "cmdb_ci", "approval", "closed_at", "assignment_group"],
  "field_stats": {"approval": {"approved": 361, "not requested": 19, "rejected": 2, "requested": 6}},
  "sample": ["CHG0031122 | Replace core switch SW-DEL-02 | not requested", "..."],
  "flags": {"injection_suspected": 0, "truncated_text_fields": 4}
}
```

## 5. Tool-use examples (for the trickiest inputs)

Per the advanced-tool-use guidance, tools with domain-specific query languages carry worked examples
in their descriptions (gateway-injected, kept short):

| Tool | Example input |
|---|---|
| `jira_issues_search_to_resultset` | `{"jql": "project = NET AND issuetype = Change AND resolutiondate >= \"2026-11-22 14:30\" AND resolutiondate < \"2026-11-23 14:30\" ORDER BY key", "fields": ["summary","status","components","resolutiondate"], "window": {"start_utc": "2026-11-22T09:00:00Z", "end_utc": "2026-11-23T09:00:00Z", "field": "resolutiondate"}, "max_records": 2000, "query_spec_id": "qs_01HZX3"}` |
| `zendesk_tickets_search_to_resultset` | `{"query": "type:ticket tags:vpn updated>=2026-11-22T09:00:00Z updated<2026-11-23T09:00:00Z", "window": {"start_utc": "2026-11-22T09:00:00Z", "end_utc": "2026-11-23T09:00:00Z", "field": "updated_at"}, "max_records": 2000, "query_spec_id": "qs_01HZX4"}` |
| `servicenow_records_search_to_resultset` | `{"table": "change_request", "encoded_query": "closed_at>=2026-11-22 09:00:00^closed_at<2026-11-23 09:00:00^ORDERBYsys_id", "fields": ["number","short_description","approval","closed_at"], "window": {"start_utc": "2026-11-22T09:00:00Z", "end_utc": "2026-11-23T09:00:00Z", "field": "closed_at"}, "max_records": 2000, "query_spec_id": "qs_01HZX3"}` |
| `analyzer_aggregate` | `{"handle": "rs_01HZX3KC", "filters": [{"field": "approval", "op": "neq", "value": "approved"}], "group_by": ["assignment_group"], "metric": "count", "label_filter": "network_change_v3:network_change"}` |

Exact query syntax (Zendesk date operators, ServiceNow encoded-query time zone, JQL date format) must
be confirmed against each vendor's current reference during build; the connector's field-catalogue
validation and integration tests (doc 11) catch drift.

## 6. Tool loading strategy

- **Always loaded** (non-deferred): `analyzer_*`, harness tools (`plan_query_spec`, `ledger_update`, `memory_*`), and the tool-search tool.
- **Deferred** (`defer_loading: true`): every MCP connector tool. The model finds them via tool search
  (`tool_search_tool_bm25_20251119`), which appends definitions without invalidating the cache.
- **Role filtering** happens before tool search: a role only ever sees tools in its `allowed_roles`.
- Total loaded definitions stay < 8k tokens regardless of how many servers are onboarded (doc 03 §2).
