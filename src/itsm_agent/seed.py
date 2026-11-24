"""Bootstrap data: roles, an admin, the built-in tool catalog, the network-change rubric, a playbook and
an approved org profile. Idempotent."""
from __future__ import annotations

from . import db
from .tools import registry

ADMIN_SUB = "system-admin"

RUBRIC = {
    "description": "Is this change a network change? A network change alters network devices, routing, switching, "
                   "firewalls, load balancers, DNS, IP addressing, VLANs or VPN infrastructure.",
    "positive_label": "network_change", "negative_label": "not_network_change",
    "rules": ["Network devices (switch, router, firewall, load balancer, DNS server) being changed -> network_change.",
              "Routing (BGP, OSPF), VLAN, subnet/IP re-addressing, VPN gateway changes -> network_change.",
              "Printers, desktops, applications, databases and certificates are not network changes, even if the text says 'network printer'.",
              "If the record does not say what was changed, use insufficient_information."],
    "examples": [{"text": "Replaced core switch SW-DEL-02 in rack B4", "label": "network_change", "evidence": "Replaced core switch SW-DEL-02"},
                 {"text": "Network printer driver update on floor 7", "label": "not_network_change", "evidence": "Network printer driver update"},
                 {"text": "Patched application server 12", "label": "not_network_change", "evidence": "Patched application server"}],
    "prefilter": {"include_if": [{"field": "category", "op": "eq", "value": "network"},
                                 {"field": "components", "op": "contains", "value": "Network"}],
                  "exclude_if": [{"field": "category", "op": "in", "value": "application,database,hardware"}]},
    "recall_lexicon": ["firewall", "router", "switch", "vlan", "bgp", "dns", "subnet", "vpn", "load balancer", "network"],
    "second_opinion_sample": 0.05,
}

PLAYBOOK = """Playbook: CAB compliance check for changes
Use when the user asks about changes (optionally of a kind, e.g. network) in a time window and whether they had CAB approval.
1. plan_query_spec(window_field="closed" unless the user says created/updated; systems=["servicenow","jira"]; rubric_id="network_change_v3" for network questions).
2. servicenow_records_search_to_resultset(table="change_request", encoded_query="", fields=["number","short_description","category","approval","assignment_group","cmdb_ci","closed_at"]).
3. jira_issues_search_to_resultset(jql="project in (NET, OPS) AND issuetype = Change", fields=["summary","components","resolutiondate"]).
4. analyzer_resultset_union(handles=[servicenow handle, jira handle], dedupe_key="change_ref").
5. analyzer_classify(handle=union, rubric_id="network_change_v3").
6. analyzer_aggregate(metric="count", label_filter="network_change_v3:network_change") -> total network changes.
7. analyzer_aggregate(metric="count", label_filter="network_change_v3:network_change", filters=[{"field":"cab_status","op":"neq","value":"approved"}]) -> without CAB.
8. analyzer_list_create(columns=["source_id","title","cab_status","source_system"], same filters, label_filter) for the list.
9. submit_answer with both numbers as numeric claims and the list placeholder.
CAB status: ServiceNow change_request.approval; Jira CAB field. A Jira change linked to a ServiceNow change is the same change."""


def run() -> dict:
    with db.tx() as c:
        c.execute("INSERT INTO orgs (id, name) VALUES ('00000000-0000-0000-0000-000000000001', 'Default org') ON CONFLICT DO NOTHING")
        for r, d in [("requester", "Can ask and propose"), ("approver", "Approves high-risk actions"),
                     ("admin", "Platform admin"), ("security_reviewer", "Reviews risky MCP servers")]:
            c.execute("INSERT INTO roles VALUES (%s,%s) ON CONFLICT DO NOTHING", (r, d))
        admin = c.execute("""INSERT INTO users (org_id, external_subject, email, display_name)
                             VALUES ('00000000-0000-0000-0000-000000000001', %s, 'admin@localhost', 'Platform admin')
                             ON CONFLICT (external_subject) DO UPDATE SET display_name = EXCLUDED.display_name RETURNING id""",
                          (ADMIN_SUB,)).fetchone()["id"]
        for role in ("admin", "approver"):
            c.execute("INSERT INTO user_roles (user_id, role) VALUES (%s,%s) ON CONFLICT DO NOTHING", (admin, role))
        c.execute("""INSERT INTO rubrics (id, name, version, labels, definition, status, approved_by)
                     VALUES ('network_change_v3','network_change',3,%s,%s,'active',%s) ON CONFLICT (id) DO NOTHING""",
                  (["network_change", "not_network_change", "insufficient_information"], db.j(RUBRIC), admin))
        c.execute("""INSERT INTO playbooks (name, version, description, body_md, rubric_ids, required_servers, status, approved_by)
                     VALUES ('cab_compliance_check', 1, 'changes in a time window with CAB approval status, network changes', %s,
                             ARRAY['network_change_v3'], ARRAY['servicenow','jira','analyzer'], 'active', %s)
                     ON CONFLICT (name, version) DO NOTHING""", (PLAYBOOK, admin))
        for key, content, refs in [
            ("change.network.where", "Network changes are tracked in ServiceNow change_request (category network, group NOC-L2) and Jira projects NET and OPS (issuetype Change).", ["admin-entered"]),
            ("change.cab.field", "CAB approval is ServiceNow change_request.approval = approved; in Jira the CAB approval custom field = Approved.", ["admin-entered"])]:
            c.execute("""INSERT INTO memory_items (kind, scope, subject_key, content, status, confidence, source_refs, reviewed_by, reviewed_at, pinned)
                         SELECT 'semantic','org',%s,%s,'active',0.95,%s,%s,now(),true
                         WHERE NOT EXISTS (SELECT 1 FROM memory_items WHERE subject_key = %s AND status = 'active')""",
                      (key, content, db.j(refs), admin, key))
    registry.sync_to_db(str(admin))
    return {"admin_user_id": str(admin)}


def ensure_user(subject: str, email: str, name: str, tz: str = "UTC", roles: tuple[str, ...] = ("requester",)) -> str:
    with db.tx() as c:
        uid = c.execute("""INSERT INTO users (org_id, external_subject, email, display_name, time_zone)
                           VALUES ('00000000-0000-0000-0000-000000000001', %s, %s, %s, %s)
                           ON CONFLICT (external_subject) DO UPDATE SET time_zone = EXCLUDED.time_zone RETURNING id""",
                        (subject, email, name, tz)).fetchone()["id"]
        for r in roles:
            c.execute("INSERT INTO user_roles (user_id, role) VALUES (%s,%s) ON CONFLICT DO NOTHING", (uid, r))
    return str(uid)
