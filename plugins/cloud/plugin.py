import json

from orchestrator.plugins import post_job


def list_modules(conn):
    return post_job("ORCH_TOFU", {"capability": "cloud.modules.list"}, 30)["modules"]


def plan_module(conn, module, workspace, vars=None):
    plan = post_job("ORCH_TOFU", {"capability": "cloud.module.apply", "phase": "plan", "module": module,
                                  "workspace": workspace, "vars": vars or {}}, 1900)
    # In the shape every change plan has: one target (the workspace), one task per resource change.
    tasks = [{"task": f"{c['address']} ({', '.join(c['actions'])})", "changed": True,
              "diff": json.dumps({"before": c["before"], "after": c["after"]}, indent=1, sort_keys=True)}
             for c in plan["changes"]]
    return {"digest": plan["digest"], "rollback": plan["rollback"], "targets": [],
            "hosts": {f"{module}/{workspace}": {"status": "ok", "tasks": tasks}}}


def apply_module(digest, module, workspace, vars=None):
    return post_job("ORCH_TOFU", {"capability": "cloud.module.apply", "phase": "apply", "digest": digest,
                                  "module": module, "workspace": workspace, "vars": vars or {}}, 5500)
