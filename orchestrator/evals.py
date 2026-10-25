"""Evals: prompts with an expected course of tool calls, run against the configured model.

Each case gets its own throwaway tenant with the devices it names, and every capability that would reach outside the
database answers from the case's canned results, so an eval never touches a device, a cloud or a monitoring system.
What the model called is read back from the audit ledger: refused calls count as calls.
"""
import uuid
from pathlib import Path

import yaml

from . import db, loop, plugins

LOCAL = ("inventory.", "skills.")  # these only read the tenant's own rows, so they run for real
# shortcut: each run leaves its eval-* tenants behind (the ledger is append-only); point evals at a scratch database.


def canned(cap, results):
    def handler(conn, **args):
        if cap["id"] in results:
            return results[cap["id"]]
        if cap["kind"] == "change":
            targets = args.get("devices", [])
            return {"digest": "0" * 64, "rollback": True, "targets": targets,
                    "hosts": {d: {"status": "ok", "tasks": []} for d in targets}}
        return {"note": "no data for this question"}
    return {**cap, "handler": handler, "apply": None}


def called(tenant_id, request_id):
    with db.tenant(tenant_id) as conn:
        rows = conn.execute("SELECT kind, data FROM audit WHERE data->>'request' = %s ORDER BY seq",
                            (str(request_id),)).fetchall()
    return [r["data"]["capability"] if r["kind"] in ("tool.read", "change.planned")
            else r["data"]["tool"].replace("__", ".") if r["kind"] == "tool.refused" else "agents.delegate"
            for r in rows if r["kind"] in ("tool.read", "change.planned", "tool.refused", "agent.delegated")]


def run_case(case, chat=None):
    """Returns (problems, calls); no problems is a pass."""
    tenant_id = db.create_tenant(f"eval-{case['name']}-{uuid.uuid4().hex[:8]}", "admin")
    role = case.get("role", "operator")
    with db.tenant(tenant_id) as conn:
        conn.execute("INSERT INTO users (tenant_id, name, role) VALUES (current_setting('app.tenant_id')::uuid, "
                     "'asker', %s)", (role,))
        for d in case.get("devices", []):
            conn.execute("INSERT INTO devices (tenant_id, name, address, platform, site) VALUES "
                         "(current_setting('app.tenant_id')::uuid, %s, '192.0.2.1', %s, %s)",
                         (d["name"], d.get("platform", "ios"), d.get("site", "")))
    caps = {k: c if k.startswith(LOCAL) else canned(c, case.get("results", {})) for k, c in plugins.load().items()}
    request_id, state, answer = loop.run(tenant_id, "asker", case["prompt"], chat=chat, caps=caps,
                                         reviewer=lambda m, t: {"content": "No concerns."})
    calls = called(tenant_id, request_id)
    problems = [f"ended {state}: {answer[:200]}"] if state != "DONE" else []
    problems += [f"did not call {c}" for c in case.get("expect", []) if c not in calls]
    problems += [f"called {c}" for c in case.get("never", []) if c in calls]
    problems += [f"answer lacks {s!r}" for s in case.get("answer_has", []) if s.lower() not in answer.lower()]
    return problems, calls


def files():
    """The shipped cases: evals/cases.yaml and each shipped skill's evals.yaml."""
    root = Path(__file__).resolve().parent.parent
    return [root / "evals" / "cases.yaml", *sorted((root / "skills").glob("*/evals.yaml"))]


def run(paths=None, chat=None):
    """Run every case in the YAML files (the shipped ones by default). Returns True when all pass."""
    passed = True
    for case in [c for p in (paths or files()) for c in yaml.safe_load(open(p))]:
        problems, calls = run_case(case, chat)
        passed &= not problems
        print(f"{'FAIL' if problems else 'PASS'} {case['name']}: {', '.join(calls) or 'no calls'}"
              + "".join(f"\n  - {p}" for p in problems))
    return passed
