"""Skills: procedures the agent reads when a task needs one. A SKILL.md is YAML front matter (name, description,
capabilities) and a Markdown body.

Three sources. Shipped skills are folders under skills/ in the release. A tenant administrator adds the tenant's own
(`skill-add`). The agent may propose one after a task (`skills.propose`); it stays inert, never listed or readable,
until a tenant administrator approves it. A skill is text: it grants nothing, and every tool call it leads to is
still checked by the policy.
"""
import re
from pathlib import Path

import yaml

from . import db, guard, policy

SHIPPED_DIR = Path(__file__).resolve().parent.parent / "skills"
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


def parse(text):
    """{name, description, capabilities, body} from a SKILL.md. Raises ValueError with the first problem found."""
    match = re.fullmatch(r"---\n(.*?)\n---\n(.*)", text, re.S)
    meta = yaml.safe_load(match[1]) if match else None
    if not isinstance(meta, dict) or set(meta) - {"name", "description", "capabilities"}:
        raise ValueError("a skill starts with --- front matter holding name, description and capabilities ---")
    return check(meta.get("name"), meta.get("description"), match[2].strip(), meta.get("capabilities", []))


def check(name, description, body, capabilities):
    if not (isinstance(name, str) and NAME.fullmatch(name)):
        raise ValueError("a skill name is lower-case letters, digits and '-'")
    if not (isinstance(description, str) and 0 < len(description) <= 300):
        raise ValueError("a skill needs a description of 1 to 300 characters")
    if not (isinstance(body, str) and 0 < len(body) <= 20000):
        raise ValueError("a skill needs a body of 1 to 20000 characters")
    if not (isinstance(capabilities, list) and all(isinstance(c, str) for c in capabilities)):
        raise ValueError("capabilities is a list of capability ids")
    return {"name": name, "description": description, "capabilities": capabilities, "body": guard.redact(body)}


def shipped():
    return {s["name"]: s for s in (parse(p.read_text()) for p in sorted(SHIPPED_DIR.glob("*/SKILL.md")))}


def usable(conn):
    """Shipped skills and the tenant's approved ones, by name."""
    rows = conn.execute("SELECT name, description, capabilities, body FROM skills WHERE state = 'approved'")
    return {**{r["name"]: r for r in rows}, **shipped()}  # a shipped name always wins


def index(conn, pol, role):
    """The skills worth offering this role: every capability they name is one it may use."""
    return {name: s["description"] for name, s in sorted(usable(conn).items())
            if all(policy.may_use(pol, role, c) for c in s["capabilities"])}


def add(conn, actor, skill, source, state):
    if skill["name"] in shipped() or conn.execute("SELECT 1 FROM skills WHERE name = %s", (skill["name"],)).fetchone():
        raise ValueError(f"a skill named {skill['name']!r} already exists")
    conn.execute("INSERT INTO skills (tenant_id, name, description, body, capabilities, source, state, proposed_by, "
                 "decided_by) VALUES (current_setting('app.tenant_id')::uuid, %s, %s, %s, %s, %s, %s, %s, %s)",
                 (skill["name"], skill["description"], skill["body"], skill["capabilities"], source, state, actor,
                  actor if state == "approved" else None))
    db.audit(conn, actor, f"skill.{state}", {"skill": skill["name"], "source": source, "digest": db.digest(skill)})


def decide(conn, user, name, approve):
    """A tenant administrator approves a proposed skill, or retires any tenant skill."""
    if user["role"] != "tenant-admin":
        raise PermissionError("only a tenant administrator can approve or retire a skill")
    state = "approved" if approve else "retired"
    if not conn.execute("UPDATE skills SET state = %s, decided_by = %s WHERE name = %s AND state <> 'retired' "
                        "AND (%s = 'retired' OR state = 'proposed')", (state, user["name"], name, state)).rowcount:
        raise LookupError(f"no {'proposed' if approve else 'active'} skill {name!r}")
    db.audit(conn, user["name"], f"skill.{state}", {"skill": name})


def listing(conn):
    return conn.execute("SELECT name, description, body, capabilities, source, state, proposed_by, decided_by, "
                        "created_at FROM skills ORDER BY state = 'proposed' DESC, created_at DESC").fetchall()
