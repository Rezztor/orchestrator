"""Rezztor Orchestrator's cloud executor: reviewed OpenTofu modules (Azure through the azurerm provider, or any other
provider the customer installs), with the credentials kept here, never in the core.

The core sends a module name, a workspace name and variables. A module is a folder of .tf files in ORCH_TOFU_MODULES,
with a README.md whose first line says what it does. Each module and workspace gets its own working folder under
ORCH_TOFU_STATE, which holds its local state unless the module declares a backend.

A plan is `tofu plan` saved and shown as JSON, with sensitive values masked, and a digest of the module's files, the
workspace, the variables and the planned resource changes. An apply carries the approved digest: this service plans
again and refuses on any difference, applies that saved plan, and verifies that a fresh plan finds nothing left to
change. There is no automatic rollback for cloud resources, so the policy counts these changes one risk class higher.

    ORCH_TOFU_TOKEN        shared job token, at least 32 characters (required)
    ORCH_TOFU_MODULES      folder of reviewed modules (required)
    ORCH_TOFU_STATE        folder for working copies and local state (required)
    ORCH_TOFU_LISTEN       host:port, default 127.0.0.1:8471
    ORCH_TOFU_CERT/KEY     serve HTTPS with this certificate and key
    ARM_* and the rest     provider credentials, read by OpenTofu itself

python -m executors.tofu_executor
"""
import fcntl
import json
import os
import re
import shutil
import subprocess
from hashlib import sha256
from pathlib import Path

from . import service
from .service import JobHandler

NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
VAR = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
TIMEOUT = 1800


def tofu(workdir, *args, ok=(0,)):
    flags = ["-no-color"] + ([] if args[0] == "show" else ["-input=false"])  # before any positional argument
    proc = subprocess.run(["tofu", args[0], *flags, *args[1:]], cwd=workdir, capture_output=True, text=True,
                          timeout=TIMEOUT, env={**os.environ, "TF_IN_AUTOMATION": "1"})
    if proc.returncode not in ok:
        raise RuntimeError(f"tofu {args[0]} exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[-800:]}")
    return proc


def list_modules(folder):
    out = []
    for path in sorted(Path(folder).iterdir()):
        if path.is_dir() and NAME.fullmatch(path.name) and any(path.glob("*.tf")):
            readme = path / "README.md"
            first = readme.read_text().strip().splitlines()[0].lstrip("# ") if readme.is_file() else ""
            out.append({"name": path.name, "description": first[:300]})
    return out


def mask(value, sensitive):
    """The value with what OpenTofu marks sensitive replaced, so it never leaves this service."""
    if sensitive is True:
        return "(sensitive)"
    if isinstance(value, dict) and isinstance(sensitive, dict):
        return {k: mask(v, sensitive.get(k)) for k, v in value.items()}
    if isinstance(value, list) and isinstance(sensitive, list):
        return [mask(v, sensitive[i] if i < len(sensitive) else None) for i, v in enumerate(value)]
    return value


def changes(workdir):
    shown = json.loads(tofu(workdir, "show", "-json", "orch.plan").stdout)
    return [{"address": r["address"], "actions": r["change"]["actions"],
             "before": mask(r["change"].get("before"), r["change"].get("before_sensitive")),
             "after": mask(r["change"].get("after"), r["change"].get("after_sensitive"))}
            for r in shown.get("resource_changes", []) if r["change"]["actions"] != ["no-op"]]


def module_job(body, modules, state):
    """Plan, or apply an approved plan, of one reviewed module in one workspace. See the module docstring."""
    name, workspace, phase = body.get("module"), body.get("workspace"), body.get("phase")
    values = body.get("vars") or {}
    if not (isinstance(name, str) and NAME.fullmatch(name) and (Path(modules) / name).is_dir()):
        raise ValueError("no such module")
    if not (isinstance(workspace, str) and NAME.fullmatch(workspace)):
        raise ValueError("a workspace name is lower-case letters, digits, '_' and '-'")
    if not (isinstance(values, dict) and len(values) <= 50 and all(
            VAR.fullmatch(k) and isinstance(v, (str, int, bool)) for k, v in values.items())):
        raise ValueError("vars maps up to 50 names to plain strings, numbers or true/false")
    if phase not in ("plan", "apply"):
        raise ValueError("phase is plan or apply")
    workdir = Path(state) / name / workspace
    workdir.mkdir(parents=True, exist_ok=True)
    with open(workdir / ".orch.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # one job per module and workspace at a time: they share the working copy
        return run_module(body, Path(modules) / name, workdir, workspace, values, phase)


def run_module(body, source, workdir, workspace, values, phase):
    for old in workdir.glob("*.tf"):  # the working copy is exactly the reviewed module, nothing left from before
        old.unlink()
    files = sorted(source.glob("*.tf"))
    for path in files:
        shutil.copyfile(path, workdir / path.name)
    (workdir / "orch.tfvars.json").write_text(json.dumps(values))
    tofu(workdir, "init", "-lock-timeout=60s")

    def plan():
        tofu(workdir, "plan", "-lock-timeout=60s", "-out=orch.plan", "-var-file=orch.tfvars.json")
        return changes(workdir)

    planned = plan()
    digest = sha256(json.dumps({
        "module": source.name, "files": {p.name: sha256(p.read_bytes()).hexdigest() for p in files},
        "workspace": workspace, "vars": values, "changes": planned}, sort_keys=True).encode()).hexdigest()
    if phase == "plan":
        return {"digest": digest, "rollback": False, "changes": planned}
    if body.get("digest") != digest:
        return {"outcome": "superseded", "digest": digest, "changes": planned}
    out = {"digest": digest}
    try:
        out["applied"] = tofu(workdir, "apply", "-lock-timeout=60s", "orch.plan").stdout.strip()[-2000:]
    except RuntimeError as exc:
        return {"outcome": "failed", **out, "error": str(exc)}
    check = tofu(workdir, "plan", "-lock-timeout=60s", "-detailed-exitcode", "-var-file=orch.tfvars.json", ok=(0, 2))
    out["verify"] = "nothing left to change" if check.returncode == 0 else "a fresh plan still finds changes"
    return {"outcome": "verified" if check.returncode == 0 else "failed", **out}



class Handler(JobHandler):
    modules = state = None

    def handle_job(self, body):
        if body.get("capability") == "cloud.modules.list":
            return {"modules": list_modules(self.modules)}
        if body.get("capability") == "cloud.module.apply":
            return module_job(body, self.modules, self.state)
        raise ValueError(f"unknown capability {body.get('capability')!r}")


def serve():
    Handler.modules, Handler.state = os.environ["ORCH_TOFU_MODULES"], os.environ["ORCH_TOFU_STATE"]
    service.serve(Handler, "ORCH_TOFU", "127.0.0.1:8471")


if __name__ == "__main__":
    serve()
