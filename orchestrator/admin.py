"""What the page's screens read, and what a tenant administrator changes from it: request history, the overview, the
ledger, people, devices, policy and the tenant's notifications and triggers.

Settings that name the server's own secrets (a tenant's executor or model route, directory sign-in) and the ones
outside a tenant (the SIEM, signing keys) stay with the installation's owner on the command line: a tenant
administrator who could name a secret could point it at their own server and read it.
"""
import os
import re

from . import auth, changes, db, mcp, policy

ROLES = ("tenant-admin", "approver", "operator", "viewer")
NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


def history(conn, me):
    """The signed-in person's 50 latest requests."""
    return conn.execute("SELECT r.id, left(r.prompt, 160) AS prompt, r.state, r.created_at FROM requests r "
                        "JOIN users u ON u.id = r.user_id WHERE u.name = %s ORDER BY r.created_at DESC LIMIT 50",
                        (me["user"],)).fetchall()


def overview(conn):
    pol = policy.for_tenant(conn)

    def count(sql):
        return {r["state"]: r["n"] for r in conn.execute(sql)}
    head = conn.execute("SELECT seq, at FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
    anchor = conn.execute("SELECT at FROM audit WHERE kind = 'audit.anchor' ORDER BY seq DESC LIMIT 1").fetchone()
    return {
        "requests_24h": count("SELECT state, count(*) AS n FROM requests WHERE created_at > now() - interval '24 hours' "
                              "GROUP BY state"),
        "changes_7d": count("SELECT state, count(*) AS n FROM changes WHERE updated_at > now() - interval '7 days' "
                            "GROUP BY state"),
        "waiting": conn.execute("SELECT count(*) AS n FROM changes WHERE state = 'PLANNED' AND expires_at > now()"
                                ).fetchone()["n"],
        "tokens_24h": conn.execute("SELECT coalesce(sum(tokens), 0) AS n FROM requests "
                                   "WHERE created_at > now() - interval '24 hours'").fetchone()["n"],
        "tokens_limit": pol["limits"]["max_tokens_per_day"],
        "per_hour": [r["n"] for r in conn.execute(
            "SELECT count(r.id) AS n FROM generate_series(0, 23) h LEFT JOIN requests r "
            "ON r.created_at > now() - make_interval(hours => 24 - h) "
            "AND r.created_at <= now() - make_interval(hours => 23 - h) GROUP BY h ORDER BY h")],
        "recent_changes": changes.listing(conn)[:5],
        "ledger": {"rows": head and head["seq"], "last_at": head and head["at"],
                   "last_anchor_at": anchor and anchor["at"]},
        "stopped": policy.stopped(conn),
    }


def ledger_page(conn, before=None, kind=None, actor=None, ref=None):
    """50 ledger rows, newest first, with the chain's state."""
    rows = conn.execute(
        "SELECT seq, at, actor, kind, data, hash FROM audit WHERE (%(before)s::bigint IS NULL OR seq < %(before)s) "
        "AND (%(kind)s::text IS NULL OR kind LIKE %(kind)s || '%%') AND (%(actor)s::text IS NULL OR actor = %(actor)s) "
        "AND (%(ref)s::text IS NULL OR data->>'request' = %(ref)s OR data->>'change' = %(ref)s) "
        "ORDER BY seq DESC LIMIT 50", {"before": before, "kind": kind, "actor": actor, "ref": ref}).fetchall()
    # shortcut: the chain is recomputed on every load; cache it per head seq when ledgers reach millions of rows
    broken = db.verify_audit(conn)
    anchors = conn.execute("SELECT count(*) AS n, max(at) AS last FROM audit WHERE kind = 'audit.anchor'").fetchone()
    total = conn.execute("SELECT count(*) AS n FROM audit").fetchone()["n"]
    return {"rows": rows, "chain": {"holds": broken is None, "broken_at": broken, "rows": total,
                                    "anchors": anchors["n"], "last_anchor_at": anchors["last"]}}


def people(conn):
    seen = {r["actor"]: r["at"] for r in conn.execute(
        "SELECT actor, max(at) AS at FROM audit WHERE kind = 'auth.signed_in' GROUP BY actor")}
    return {"directory": conn.execute("SELECT 1 FROM oidc").fetchone() is not None,
            "people": [{**r, "last_seen": seen.get(r["name"])} for r in conn.execute(
                "SELECT name, role, password_hash IS NOT NULL AS password, totp_secret IS NOT NULL AS authenticator "
                "FROM users ORDER BY name")]}


def add_person(tenant_id, actor, name, role, password=None):
    if not NAME.fullmatch(name or "") or role not in ROLES:
        raise ValueError(f"a name is lower-case letters, digits, '.', '_' or '-'; a role is one of {', '.join(ROLES)}")
    if password is not None and len(password) < 12:
        raise ValueError("a password needs at least 12 characters")
    with db.tenant(tenant_id) as conn:
        if conn.execute("INSERT INTO users (tenant_id, name, role) VALUES (current_setting('app.tenant_id')::uuid, "
                        "%s, %s) ON CONFLICT DO NOTHING", (name, role)).rowcount != 1:
            raise ValueError(f"{name} is already here")
        db.audit(conn, actor, "user.added", {"user": name, "role": role})
    if password is not None:
        auth.set_password(tenant_id, name, password)


def set_role(tenant_id, actor, name, role):
    """Change someone's role and end their sessions, so a new approver signs in again with an authenticator code."""
    if role not in ROLES:
        raise ValueError(f"a role is one of {', '.join(ROLES)}")
    if name == actor:
        raise PermissionError("another administrator changes your own role")
    with db.tenant(tenant_id) as conn:
        if conn.execute("UPDATE users SET role = %s WHERE name = %s", (role, name)).rowcount != 1:
            raise LookupError(f"no user {name!r}")
        conn.execute("DELETE FROM sessions WHERE user_id = (SELECT id FROM users WHERE name = %s)", (name,))
        db.audit(conn, actor, "user.role", {"user": name, "role": role})


def devices(conn):
    return conn.execute("SELECT name, address, platform, site, tags, critical FROM devices ORDER BY name").fetchall()


def add_device(conn, actor, name, address, platform, site=""):
    if not all(isinstance(v, str) and v.strip() and len(v) <= 200 for v in (name, address, platform)):
        raise ValueError("a device needs a name, an address and a platform")
    if conn.execute("INSERT INTO devices (tenant_id, name, address, platform, site) VALUES "
                    "(current_setting('app.tenant_id')::uuid, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (name.strip(), address.strip(), platform.strip(), (site or "").strip()[:200])).rowcount != 1:
        raise ValueError(f"{name} is already in the inventory")
    db.audit(conn, actor, "device.added", {"device": name.strip()})


def set_critical(conn, actor, name, critical):
    if conn.execute("UPDATE devices SET critical = %s WHERE name = %s", (critical, name)).rowcount != 1:
        raise LookupError(f"no device {name!r}")
    db.audit(conn, actor, "device.critical", {"device": name, "critical": critical})


def policy_view(conn):
    row = conn.execute("SELECT document, set_by, set_at FROM policies").fetchone()
    return {"effective": policy.for_tenant(conn), "shipped": policy.SHIPPED, "document": row and row["document"],
            "set_by": row and row["set_by"], "set_at": row and row["set_at"]}


def set_policy(conn, actor, text):
    """The tenant's own policy (YAML). Raises ValueError when it does not parse; it can only narrow the shipped one."""
    policy.parse(text)
    conn.execute("INSERT INTO policies (tenant_id, document, set_by) VALUES (current_setting('app.tenant_id')::uuid, "
                 "%s, %s) ON CONFLICT (tenant_id) DO UPDATE SET document = EXCLUDED.document, set_by = EXCLUDED.set_by, "
                 "set_at = now()", (text, actor))
    db.audit(conn, actor, "policy.set", {"digest": db.digest(text)})


def connections(conn, tenant_id):
    """What this tenant is connected to. Never a secret: only whether one is set."""
    routes = {r["kind"]: r for r in conn.execute("SELECT kind, url, secret_env, model FROM routes")}

    def endpoint(kind, prefix):
        own = routes.get(kind)
        if own:
            return {"url": own["url"], "own": True, "secret_set": bool(own["secret_env"] and
                                                                         os.environ.get(own["secret_env"]))}
        url = os.environ.get(f"{prefix}_URL")
        return url and {"url": url, "own": False, "secret_set": bool(os.environ.get(f"{prefix}_TOKEN" if kind != "model"
                                                                                     else f"{prefix}_KEY"))}

    model = endpoint("model", "ORCH_MODEL")
    if model:
        model["model"] = routes["model"]["model"] if model["own"] else os.environ.get("ORCH_MODEL")
        model["reviewer"] = None if model["own"] else os.environ.get("ORCH_REVIEW_MODEL")
    directory = conn.execute("SELECT issuer, client_id, username_claim FROM oidc").fetchone()
    servers = {}
    if os.environ.get("ORCH_MCP_PLUGINS"):
        for cap in mcp.load(os.environ["ORCH_MCP_PLUGINS"]).values():
            if str(tenant_id) in cap["tenants"]:
                servers.setdefault(cap["plugin"].removeprefix("mcp:"), []).append(cap["id"])
    anchor = conn.execute("SELECT max(at) AS at FROM audit WHERE kind = 'audit.anchor'").fetchone()["at"]
    return {
        "model": model,
        "ansible": endpoint("ansible", "ORCH_ANSIBLE"),
        "tofu": endpoint("tofu", "ORCH_TOFU"),
        "directory": directory,
        "servicenow": os.environ.get("ORCH_SNOW_URL") or None,
        "change_record": policy.for_tenant(conn)["change_record"],
        "solarwinds": os.environ.get("ORCH_SOLARWINDS_URL") or None,
        "mcp": [{"name": k, "tools": v} for k, v in sorted(servers.items())],
        # a webhook's address carries its secret: only the host is shown
        "webhooks": [{"id": r["id"], "kind": r["kind"], "host": r["url"].split("/")[2]} for r in conn.execute(
            "SELECT id, kind, url FROM webhooks ORDER BY kind, url")],
        "triggers": [{**r, "id": r["id"][:12]} for r in conn.execute(
            "SELECT token_hash AS id, recipe, run_as, created_by, created_at FROM triggers ORDER BY created_at")],
        "last_anchor_at": anchor,
    }


def remove_webhook(conn, actor, webhook_id):
    if conn.execute("DELETE FROM webhooks WHERE id = %s RETURNING kind", (webhook_id,)).fetchone() is None:
        raise LookupError("no such webhook")
    db.audit(conn, actor, "webhook.removed", {"webhook": str(webhook_id)})


def remove_trigger(conn, actor, trigger_id):
    """Revoke a trigger by the first 12 characters of its hash (all the page ever sees)."""
    if not re.fullmatch(r"[0-9a-f]{12}", trigger_id or ""):
        raise ValueError("a trigger id is 12 hexadecimal characters")
    row = conn.execute("DELETE FROM triggers WHERE left(token_hash, 12) = %s RETURNING recipe", (trigger_id,)).fetchone()
    if row is None:
        raise LookupError("no such trigger")
    db.audit(conn, actor, "trigger.removed", {"recipe": row["recipe"]})

