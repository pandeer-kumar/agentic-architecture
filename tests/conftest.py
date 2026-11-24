"""Test harness: a fresh PostgreSQL database, the fake vendor APIs (pinned clock) and the fake Claude API.
Set ITSM_TEST_ADMIN_URL to a superuser DSN (default: local socket used in development)."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import psycopg
import pytest

from itsm_agent import config, db, llm, seed
from itsm_agent.connectors import factory
from itsm_agent.testing.fake_claude import Brain, FakeClaudeServer

ADMIN = os.environ.get("ITSM_TEST_ADMIN_URL", "postgresql://postgres@/postgres?host=/var/lib/postgresql/pgtest&port=5499")
NOW = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)


def _db_url(name: str) -> str:
    base, _, query = ADMIN.partition("?")
    return base.rsplit("/", 1)[0] + "/" + name + (("?" + query) if query else "")


@pytest.fixture(scope="session")
def fake_claude():
    srv = FakeClaudeServer(Brain()).start()
    yield srv
    srv.stop()


@pytest.fixture(scope="session", autouse=True)
def database(fake_claude):
    name = f"itsm_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f"CREATE DATABASE {name}")
    config.set_settings(config.Settings(database_url=_db_url(name), connector_mode="fixture",
                                        anthropic_base_url=fake_claude.url, anthropic_api_key="fake-key",
                                        fixed_now=NOW.isoformat()))
    llm.reset_gateway()
    db.migrate()
    info = seed.run()
    factory.fake_vendors(reset=True, now=NOW)
    yield info
    db.close_pool()
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture
def brain(fake_claude):
    """Reset fault injection on the shared fake model for each test."""
    fake_claude.brain.faults = {}
    fake_claude.brain.orch_faults = set()
    return fake_claude.brain


@pytest.fixture
def vendors():
    return factory.fake_vendors()


@pytest.fixture
def user():
    return seed.ensure_user(f"u-{uuid.uuid4().hex[:6]}", f"{uuid.uuid4().hex[:6]}@test", "Tester", "Asia/Kolkata")


@pytest.fixture
def approver():
    return seed.ensure_user(f"a-{uuid.uuid4().hex[:6]}", f"{uuid.uuid4().hex[:6]}@test", "Approver", "UTC", ("approver",))


@pytest.fixture
def session(user):
    from itsm_agent.harness.orchestrator import new_session
    return new_session(user)


@pytest.fixture
def ctx(session, user):
    """A ToolContext with a job, for calling tools directly."""
    from itsm_agent.harness.orchestrator import append_event
    from itsm_agent.tools.impl import ToolContext
    ev = append_event(session, "user_message", {"text": "test"})
    job = db.q1("INSERT INTO jobs (session_id, user_id, request_event, status) VALUES (%s,%s,%s,'running') RETURNING id",
                (session, user, ev))
    return ToolContext(job_id=str(job["id"]), session_id=session, user_id=user, user_tz="Asia/Kolkata", user_turn_event_id=ev)
