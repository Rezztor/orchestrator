"""PostgreSQL access.

Two roles. The owner role (ORCH_DB_URL) migrates and creates tenants. The application role orch_app
(ORCH_DB_APP_URL) does everything else, inside `tenant(tenant_id)`, and row-level security keeps it to that tenant:
a query that runs without a tenant set sees nothing.
"""
import os
import uuid
from contextlib import contextmanager
from datetime import timezone
from hashlib import sha256
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


def migrate(owner_url=None, app_password=None):
    owner_url = owner_url or os.environ["ORCH_DB_URL"]
    app_password = app_password or os.environ["ORCH_DB_APP_PASSWORD"]
    with psycopg.connect(owner_url, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'orch_app'").fetchone()
        verb = "ALTER" if exists else "CREATE"
        conn.execute(sql.SQL(verb + " ROLE orch_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD {}").format(
            sql.Literal(app_password)))
        conn.execute((Path(__file__).with_name("schema.sql")).read_text())


def create_tenant(name, admin_name, owner_url=None):
    """Create a tenant and its first tenant-admin. Owner role: this is the installation administrator's job."""
    with psycopg.connect(owner_url or os.environ["ORCH_DB_URL"]) as conn:
        tenant_id = conn.execute("INSERT INTO tenants (name) VALUES (%s) RETURNING id", (name,)).fetchone()[0]
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
        conn.execute("INSERT INTO users (tenant_id, name, role) VALUES (%s, %s, 'tenant-admin')",
                     (tenant_id, admin_name))
    return tenant_id


@contextmanager
def tenant(tenant_id, app_url=None):
    """One transaction scoped to one tenant."""
    tenant_id = str(uuid.UUID(str(tenant_id)))
    with psycopg.connect(app_url or os.environ["ORCH_DB_APP_URL"], row_factory=dict_row) as conn:
        with conn.transaction():
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
            yield conn


def audit(conn, actor, kind, data=None):
    """Append to the ledger in the caller's tenant transaction; the trigger chains the hash."""
    conn.execute(
        "INSERT INTO audit (tenant_id, actor, kind, data) "
        "VALUES (current_setting('app.tenant_id')::uuid, %s, %s, %s)",
        (actor, kind, Jsonb(data or {})))


def verify_audit(conn):
    """Recompute the tenant's chain. Returns the seq of the first broken row, or None when the chain holds."""
    prev = ""
    for row in conn.execute("SELECT seq, tenant_id, at, actor, kind, data::text AS data, prev_hash, hash "
                            "FROM audit ORDER BY seq"):
        at = row["at"].astimezone(timezone.utc)
        stamp = at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}"
        text = "|".join([prev, str(row["tenant_id"]), stamp, row["actor"], row["kind"], row["data"]])
        if row["prev_hash"] != prev or row["hash"] != sha256(text.encode()).hexdigest():
            return row["seq"]
        prev = row["hash"]
    return None
