"""Command line: itsm-agent init-db | ask | approvals | approve | execute | serve | demo"""
from __future__ import annotations

import argparse
import json
import os
import sys


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="itsm-agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="create schema and seed roles, tools, rubric, playbook")
    a = sub.add_parser("ask", help="ask a question")
    a.add_argument("text")
    a.add_argument("--user", default="dev-user")
    a.add_argument("--tz", default="UTC")
    a.add_argument("--session", default=None)
    sub.add_parser("approvals", help="list pending approvals")
    ap = sub.add_parser("approve")
    ap.add_argument("action_id")
    ap.add_argument("--user", default="system-admin")
    ap.add_argument("--reject", action="store_true")
    sub.add_parser("execute", help="execute approved actions")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8080)
    d = sub.add_parser("demo", help="end-to-end demo in fixture mode")
    d.add_argument("--fake-model", action="store_true", help="use the built-in fake Claude API (no key needed)")
    args = p.parse_args(argv)

    from . import db, seed
    if args.cmd == "init-db":
        db.migrate()
        print(json.dumps(seed.run()))
        return 0
    if args.cmd == "serve":
        import uvicorn
        uvicorn.run("itsm_agent.api:app", host="0.0.0.0", port=args.port)
        return 0
    if args.cmd == "approvals":
        for r in db.q("SELECT action_id, tool_name, risk_tier, approvals, required_approvals, preview FROM v_pending_approvals"):
            print(f"{r['action_id']}  {r['tool_name']}  tier={r['risk_tier']}  {r['approvals']}/{r['required_approvals']}  target={r['preview'].get('target')}")
        return 0
    if args.cmd == "approve":
        from .tools import executor
        uid = db.q1("SELECT id FROM users WHERE external_subject = %s", (args.user,))["id"]
        print(executor.decide(args.action_id, str(uid), "reject" if args.reject else "approve"))
        return 0
    if args.cmd == "execute":
        from .tools import executor
        print(json.dumps(executor.run_approved(), indent=2, default=str))
        return 0
    if args.cmd == "ask":
        from .harness.orchestrator import Orchestrator, new_session
        uid = seed.ensure_user(args.user, f"{args.user}@localhost", args.user, args.tz)
        sid = args.session or new_session(uid)
        out = Orchestrator().ask(sid, args.text)
        print(out.answer_md)
        print(f"\n[session {sid} · job {out.job_id} · {out.status}]")
        if out.pending_approvals:
            print("pending approvals:", ", ".join(out.pending_approvals))
        return 0 if out.status == "completed" else 1
    if args.cmd == "demo":
        return _demo(args.fake_model)
    return 1


def _demo(fake_model: bool) -> int:
    from . import db, seed
    from .config import get_settings
    if get_settings().connector_mode != "fixture":
        print("demo runs in fixture mode: set ITSM_CONNECTOR_MODE=fixture", file=sys.stderr)
        return 2
    server = None
    if fake_model:
        from .testing.fake_claude import FakeClaudeServer
        server = FakeClaudeServer().start()
        os.environ["ITSM_ANTHROPIC_BASE_URL"] = server.url
        os.environ["ITSM_ANTHROPIC_API_KEY"] = "fake-key"
        from . import config, llm
        config._settings = None
        llm.reset_gateway()
    elif not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ITSM_ANTHROPIC_API_KEY")):
        print("Set ANTHROPIC_API_KEY to use Claude, or pass --fake-model for an offline run.", file=sys.stderr)
        return 2
    db.migrate()
    seed.run()
    from .harness.orchestrator import Orchestrator, new_session
    uid = seed.ensure_user("demo-user", "demo@localhost", "Demo user", "Asia/Kolkata")
    sid = new_session(uid)
    q = "Which network changes were done in the last 24 hours without CAB approval?"
    print(f"> {q}\n")
    out = Orchestrator().ask(sid, q)
    print(out.answer_md)
    if server:
        server.stop()
    return 0 if out.status == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
