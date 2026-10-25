"""Rezztor Orchestrator's Ansible executor: a separate service, with its own credentials, that runs named jobs.

The core never sends Ansible text. It sends a capability id, device names and, for a playbook, its name and
variables; this service maps that to a fixed command, runs it against the Ansible inventory the customer keeps here
(connection details and secrets stay in this service), and returns only the fields the capability allows.

Changes are reviewed playbooks in ORCH_ANSIBLE_PLAYBOOKS: NAME.yml, and NAME.rollback.yml when it can be backed out.
A plan is a check-mode run with diffs and a digest of everything that decides the outcome. An apply carries the
approved digest: this service plans again and refuses on any difference, applies, verifies with another check-mode
run (nothing may be left to change) and runs the rollback playbook when the apply or the verification fails.

    ORCH_ANSIBLE_TOKEN      shared job token, at least 32 characters (required)
    ORCH_ANSIBLE_INVENTORY  path to the Ansible inventory (required)
    ORCH_ANSIBLE_PLAYBOOKS  folder of reviewed playbooks (without it, only reads run)
    ORCH_ANSIBLE_LISTEN     host:port, default 127.0.0.1:8470
    ORCH_ANSIBLE_CERT/KEY   serve HTTPS with this certificate and key

python -m executors.ansible_executor
"""
import difflib
import hmac
import json
import os
import re
import ssl
import subprocess
import tempfile
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

CALLBACKS = Path(__file__).with_name("ansible_callbacks")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")  # no pattern characters: a name selects exactly one host
MAX_DEVICES = 200
PLAYBOOK = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
VAR = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
TEMPLATE = re.compile(r"{{|{%|{#")  # Ansible would render it: a variable is data, never a template
RUN_TIMEOUT = 600
FACT_KEYS = {  # an allow-list: ansible_env and the rest of what "min" gathers never leave this service
    "ansible_hostname", "ansible_distribution", "ansible_distribution_version", "ansible_os_family",
    "ansible_kernel", "ansible_architecture", "ansible_net_hostname", "ansible_net_system", "ansible_net_version",
    "ansible_net_model", "ansible_net_serialnum", "ansible_net_image", "ansible_net_api",
}
CAPABILITIES = {
    "ansible.facts.read": {"args": ["-m", "ansible.builtin.gather_facts", "-a", "gather_subset=min"],
                           "keep": lambda r: {k: v for k, v in r.get("ansible_facts", {}).items() if k in FACT_KEYS}},
}


def check_devices(devices):
    if not isinstance(devices, list) or not 0 < len(devices) <= MAX_DEVICES:
        raise ValueError(f"devices must be a list of 1 to {MAX_DEVICES} names")
    if not all(isinstance(d, str) and NAME.fullmatch(d) for d in devices):
        raise ValueError("a device name may hold only letters, digits, '.', '_' and '-'")


def ansible(command, inventory, devices, extra, timeout=RUN_TIMEOUT):
    """Run ansible or ansible-playbook on the named hosts; returns what the orch_result callback printed."""
    env = {**os.environ, "ANSIBLE_CALLBACK_PLUGINS": str(CALLBACKS), "ANSIBLE_STDOUT_CALLBACK": "orch_result",
           "ANSIBLE_LOAD_CALLBACK_PLUGINS": "1", "ANSIBLE_DEPRECATION_WARNINGS": "0"}
    proc = subprocess.run([command, "-i", inventory, "--limit", ",".join(devices), *extra],
                          capture_output=True, text=True, timeout=timeout, env=env)
    lines = proc.stdout.strip().splitlines()
    try:
        return json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError:
        raise RuntimeError(f"{command} exited {proc.returncode}: {proc.stderr.strip()[-500:]}")


def run_job(capability, devices, inventory, timeout=300):
    """Run one read capability against named hosts. Returns {device: {"status", "facts" | "message"}}."""
    job = CAPABILITIES.get(capability)
    if job is None:
        raise ValueError(f"unknown capability {capability!r}")
    check_devices(devices)
    hosts = ansible("ansible", inventory, devices, ["all", *job["args"]], timeout)
    out = {}
    for device in devices:
        host = hosts.get(device)
        if host is None:
            out[device] = {"status": "not_in_inventory"}
        elif host["status"] == "ok":
            out[device] = {"status": "ok", "facts": job["keep"](host["result"])}
        else:
            out[device] = {"status": host["status"], "message": str(host["result"].get("msg", ""))[:500]}
    return out


def list_playbooks(folder):
    out = []
    for path in sorted(Path(folder).glob("*.yml")):
        if PLAYBOOK.fullmatch(path.stem):  # NAME.rollback.yml does not match
            plays = yaml.safe_load(path.read_text())
            first = plays[0] if isinstance(plays, list) and plays and isinstance(plays[0], dict) else {}
            out.append({"name": path.stem, "description": str(first.get("name", ""))[:300],
                        "rollback": path.with_name(path.stem + ".rollback.yml").is_file()})
    return out


def diff_text(diff):
    """Ansible's diff (dict or list of dicts) as text, without the file headers that can differ between runs."""
    parts = []
    for d in diff if isinstance(diff, list) else [diff] if diff else []:
        if "prepared" in d:
            parts.append(str(d["prepared"]))
        elif "before" in d or "after" in d:
            before, after = (v if isinstance(v, str) else json.dumps(v, sort_keys=True, indent=1)
                             for v in (d.get("before", ""), d.get("after", "")))
            parts.extend(difflib.unified_diff(before.splitlines(), after.splitlines(), "before", "after", lineterm=""))
    return "\n".join(parts)


def summarise(hosts, devices):
    out = {}
    for device in devices:
        host = hosts.get(device)
        out[device] = {"status": "not_in_inventory", "tasks": []} if host is None else {
            "status": host["status"],
            "tasks": [{"task": t["task"], "status": t["status"], "changed": t["changed"], "diff": diff_text(t["diff"]),
                       **({"message": str(t["msg"])[:500]} if t["status"] in ("failed", "unreachable") else {})}
                      for t in host["tasks"]]}
    return out


def all_ok(run):
    return all(h["status"] == "ok" for h in run.values())


def playbook_job(body, inventory, folder):
    """Plan, or apply an approved plan, of one reviewed playbook. See the module docstring."""
    name, devices, values, phase = body.get("playbook"), body.get("devices"), body.get("vars") or {}, body.get("phase")
    if not folder:
        raise ValueError("this executor has no playbooks folder")
    if not (isinstance(name, str) and PLAYBOOK.fullmatch(name)):
        raise ValueError("a playbook name is lower-case letters, digits, '_' and '-'")
    check_devices(devices)
    if not (isinstance(values, dict) and len(values) <= 50 and all(
            VAR.fullmatch(k) and isinstance(v, (str, int, bool)) and not TEMPLATE.search(str(v))
            for k, v in values.items())):
        raise ValueError("vars maps up to 50 names to plain strings, numbers or true/false, "
                         "without {{ }}, {% %} or {# #}")
    if phase not in ("plan", "apply"):
        raise ValueError("phase is plan or apply")
    book = Path(folder) / f"{name}.yml"
    back = book.with_name(f"{name}.rollback.yml")
    if not book.is_file():
        raise ValueError(f"no playbook {name!r}")
    with tempfile.NamedTemporaryFile("w", suffix=".json") as extra:
        json.dump({"orch": values}, extra)  # one namespace: a variable can never set ansible_* connection settings
        extra.flush()

        def run(path, check):
            return summarise(ansible("ansible-playbook", inventory, devices,
                                     [*(["--check"] if check else []), "--diff", "-e", "@" + extra.name, str(path)]),
                             devices)

        plan = run(book, True)
        digest = sha256(json.dumps({
            "playbook": name, "content": sha256(book.read_bytes()).hexdigest(),
            "rollback": sha256(back.read_bytes()).hexdigest() if back.is_file() else None,
            "devices": sorted(devices), "vars": values, "plan": plan}, sort_keys=True).encode()).hexdigest()
        if phase == "plan":
            return {"digest": digest, "rollback": back.is_file(), "hosts": plan}
        if body.get("digest") != digest:
            return {"outcome": "superseded", "digest": digest, "hosts": plan}
        out = {"digest": digest, "applied": run(book, False)}
        if all_ok(out["applied"]):
            out["verify"] = run(book, True)
            if all_ok(out["verify"]) and not any(t["changed"] for h in out["verify"].values() for t in h["tasks"]):
                return {"outcome": "verified", **out}
        if not back.is_file():
            return {"outcome": "failed", **out}
        out["rollback"] = run(back, False)
        return {"outcome": "rolled_back" if all_ok(out["rollback"]) else "rollback_failed", **out}


class Handler(BaseHTTPRequestHandler):
    token = inventory = playbooks = None

    def reply(self, code, body):
        data = json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path != "/v1/jobs":
            return self.reply(404, {"error": "not found"})
        if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), f"Bearer {self.token}".encode()):
            return self.reply(401, {"error": "bad job token"})
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= 65536:
            return self.reply(413, {"error": "body must be 1 byte to 64 KiB"})
        try:
            body = json.loads(self.rfile.read(length))
            capability = body.get("capability")
            if capability == "ansible.playbook.run":
                return self.reply(200, playbook_job(body, self.inventory, self.playbooks))
            if capability == "ansible.playbooks.list":
                return self.reply(200, {"playbooks": list_playbooks(self.playbooks) if self.playbooks else []})
            return self.reply(200, {"hosts": run_job(capability, body.get("devices"), self.inventory)})
        except (ValueError, AttributeError) as exc:
            return self.reply(400, {"error": str(exc)})
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            return self.reply(502, {"error": str(exc)})


def serve():
    Handler.token = os.environ["ORCH_ANSIBLE_TOKEN"]
    Handler.inventory = os.environ["ORCH_ANSIBLE_INVENTORY"]
    Handler.playbooks = os.environ.get("ORCH_ANSIBLE_PLAYBOOKS")
    if len(Handler.token) < 32:
        raise SystemExit("ORCH_ANSIBLE_TOKEN must be at least 32 characters")
    host, port = os.environ.get("ORCH_ANSIBLE_LISTEN", "127.0.0.1:8470").rsplit(":", 1)
    server = ThreadingHTTPServer((host, int(port)), Handler)
    if os.environ.get("ORCH_ANSIBLE_CERT"):
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(os.environ["ORCH_ANSIBLE_CERT"], os.environ["ORCH_ANSIBLE_KEY"])
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    serve()
