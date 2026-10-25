"""Recipes and the scheduler. A recipe is a saved prompt with {placeholders}; running one is asking that prompt, so
it gets exactly the user's role, the policy and the change path. A schedule runs a recipe as a named user every so
many minutes.
"""
import logging
import re
import time

import yaml
from psycopg.types.json import Jsonb

from . import changes, db, loop

PARAM = re.compile(r"{([a-z_][a-z0-9_]{0,31})}")
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


def parse(text):
    """{description, prompt} from a recipe file (YAML). Raises ValueError with the first problem found."""
    doc = yaml.safe_load(text)
    if not (isinstance(doc, dict) and set(doc) == {"description", "prompt"}
            and all(isinstance(v, str) and v.strip() for v in doc.values())):
        raise ValueError("a recipe is YAML with description and prompt (text with {placeholders})")
    if len(doc["prompt"]) > 8000:
        raise ValueError("a recipe prompt is at most 8000 characters")
    return doc


def params(prompt):
    return sorted(set(PARAM.findall(prompt)))


def fill(prompt, values):
    """The prompt with each {placeholder} replaced. Values are short plain text: no braces, no new lines."""
    if not isinstance(values, dict) or set(values) != set(params(prompt)):
        raise ValueError(f"give exactly these values: {', '.join(params(prompt)) or 'none'}")
    for key, value in values.items():
        if not (isinstance(value, str) and 0 < len(value) <= 200 and not re.search(r"[{}\n\r]", value)):
            raise ValueError(f"{key} must be 1 to 200 characters of plain text on one line")
    return PARAM.sub(lambda m: values[m[1]], prompt)


def add(tenant_id, actor, name, text):
    if not NAME.fullmatch(name):
        raise ValueError("a recipe name is lower-case letters, digits and '-'")
    doc = parse(text)
    with db.tenant(tenant_id) as conn:
        conn.execute("INSERT INTO recipes (tenant_id, name, description, prompt, created_by) VALUES "
                     "(current_setting('app.tenant_id')::uuid, %s, %s, %s, %s) ON CONFLICT (tenant_id, name) DO "
                     "UPDATE SET description = EXCLUDED.description, prompt = EXCLUDED.prompt, "
                     "created_by = EXCLUDED.created_by", (name, doc["description"], doc["prompt"], actor))
        db.audit(conn, actor, "recipe.saved", {"recipe": name, "digest": db.digest(doc)})


def listing(conn):
    return [{**r, "params": params(r["prompt"])}
            for r in conn.execute("SELECT name, description, prompt FROM recipes ORDER BY name")]


def run(tenant_id, user_name, name, values, chat=None):
    """Ask the recipe's prompt as this user. Returns what loop.run returns."""
    with db.tenant(tenant_id) as conn:
        row = conn.execute("SELECT prompt FROM recipes WHERE name = %s", (name,)).fetchone()
        if row is None:
            raise LookupError(f"no recipe {name!r}")
        prompt = fill(row["prompt"], values)
        db.audit(conn, user_name, "recipe.run", {"recipe": name, "values": values})
    return loop.run(tenant_id, user_name, prompt, chat=chat)


def schedule(tenant_id, actor, recipe, run_as, every_minutes, values):
    with db.tenant(tenant_id) as conn:
        row = conn.execute("SELECT prompt FROM recipes WHERE name = %s", (recipe,)).fetchone()
        if row is None:
            raise LookupError(f"no recipe {recipe!r}")
        fill(row["prompt"], values)  # refuse bad values now, not at three in the morning
        changes.user_row(conn, run_as)
        schedule_id = conn.execute(
            "INSERT INTO schedules (tenant_id, recipe, params, run_as, every_minutes, created_by) VALUES "
            "(current_setting('app.tenant_id')::uuid, %s, %s, %s, %s, %s) RETURNING id",
            (recipe, Jsonb(values), run_as, every_minutes, actor)).fetchone()["id"]
        db.audit(conn, actor, "schedule.added", {"schedule": str(schedule_id), "recipe": recipe, "run_as": run_as,
                                                 "every_minutes": every_minutes})
    return schedule_id


def run_due(chat=None):
    """Run every due schedule once. Claiming moves next_run_at first, so two schedulers never run one twice."""
    with db.app() as conn:
        due = conn.execute("SELECT tenant_id, id FROM due_schedules()").fetchall()
    ran = []
    for row in due:
        with db.tenant(row["tenant_id"]) as conn:
            job = conn.execute("UPDATE schedules SET next_run_at = now() + make_interval(mins => every_minutes) "
                               "WHERE id = %s AND next_run_at <= now() RETURNING recipe, params, run_as",
                               (row["id"],)).fetchone()
        if job is None:
            continue
        try:
            request_id, state, _ = run(row["tenant_id"], job["run_as"], job["recipe"], job["params"], chat=chat)
            outcome = {"request": str(request_id), "state": state}
        except Exception as exc:  # a schedule whose user or recipe has gone must not stop the others
            outcome = {"state": "FAILED", "reason": f"{type(exc).__name__}: {exc}"}
        with db.tenant(row["tenant_id"]) as conn:
            db.audit(conn, "scheduler", "schedule.ran", {"schedule": str(row["id"]), **outcome})
        ran.append(outcome)
    return ran


def scheduler(every_seconds=30):
    while True:
        try:
            run_due()
            changes.sweep_lost()
        except Exception:  # the database may be restarting: try again next round
            logging.exception("scheduler round failed")
        time.sleep(every_seconds)
