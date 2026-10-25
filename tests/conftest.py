import os

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from orchestrator import db

OWNER_URL = os.environ.get("ORCH_TEST_DB_URL")


@pytest.fixture
def database(monkeypatch):
    """A clean schema in the database ORCH_TEST_DB_URL names (owner role). Every table is dropped first."""
    if not OWNER_URL:
        pytest.skip("set ORCH_TEST_DB_URL to an owner-role URL of an empty PostgreSQL 16+ database")
    with psycopg.connect(OWNER_URL, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS sign_in_failures, siem_cursor, webhooks, schedules, recipes, skills, approvals, changes, "
                     "audit, devices, requests, sessions, policies, kill_switch, installation_kill, users, tenants "
                     "CASCADE")
    db.migrate(OWNER_URL, "test-app-password")
    monkeypatch.setenv("ORCH_DB_URL", OWNER_URL)
    monkeypatch.setenv("ORCH_DB_APP_URL", make_conninfo(OWNER_URL, user="orch_app", password="test-app-password"))
    return OWNER_URL
