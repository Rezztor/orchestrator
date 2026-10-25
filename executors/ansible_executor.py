"""Rezztor Orchestrator's Ansible executor: a separate service, with its own credentials, that runs named jobs.

The core never sends Ansible text. It sends a capability id and device names; this service maps the capability to
a fixed command, runs it against the Ansible inventory the customer keeps here (connection details and secrets
stay in this service), and returns only the fields the capability allows.

    ORCH_ANSIBLE_TOKEN      shared job token, at least 32 characters (required)
    ORCH_ANSIBLE_INVENTORY  path to the Ansible inventory (required)
    ORCH_ANSIBLE_LISTEN     host:port, default 127.0.0.1:8470
    ORCH_ANSIBLE_CERT/KEY   serve HTTPS with this certificate and key

python -m executors.ansible_executor
"""
import hmac
import json
import os
import re
import ssl
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CALLBACKS = Path(__file__).with_name("ansible_callbacks")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")  # no pattern characters: a name selects exactly one host
MAX_DEVICES = 200
FACT_KEYS = {  # an allow-list: ansible_env and the rest of what "min" gathers never leave this service
    "ansible_hostname", "ansible_distribution", "ansible_distribution_version", "ansible_os_family",
    "ansible_kernel", "ansible_architecture", "ansible_net_hostname", "ansible_net_system", "ansible_net_version",
    "ansible_net_model", "ansible_net_serialnum", "ansible_net_image", "ansible_net_api",
}
CAPABILITIES = {
    "ansible.facts.read": {"args": ["-m", "ansible.builtin.gather_facts", "-a", "gather_subset=min"],
                           "keep": lambda r: {k: v for k, v in r.get("ansible_facts", {}).items() if k in FACT_KEYS}},
}


def run_job(capability, devices, inventory, timeout=300):
    """Run one capability against named hosts. Returns {device: {"status", "facts" | "message"}}."""
    job = CAPABILITIES.get(capability)
    if job is None:
        raise ValueError(f"unknown capability {capability!r}")
    if not isinstance(devices, list) or not 0 < len(devices) <= MAX_DEVICES:
        raise ValueError(f"devices must be a list of 1 to {MAX_DEVICES} names")
    if not all(isinstance(d, str) and NAME.fullmatch(d) for d in devices):
        raise ValueError("a device name may hold only letters, digits, '.', '_' and '-'")
    env = {**os.environ, "ANSIBLE_CALLBACK_PLUGINS": str(CALLBACKS), "ANSIBLE_STDOUT_CALLBACK": "orch_result",
           "ANSIBLE_LOAD_CALLBACK_PLUGINS": "1", "ANSIBLE_DEPRECATION_WARNINGS": "0"}
    proc = subprocess.run(["ansible", "all", "-i", inventory, "--limit", ",".join(devices), *job["args"]],
                          capture_output=True, text=True, timeout=timeout, env=env)
    lines = proc.stdout.strip().splitlines()
    try:
        hosts = json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError:
        raise RuntimeError(f"ansible exited {proc.returncode}: {proc.stderr.strip()[-500:]}")
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


class Handler(BaseHTTPRequestHandler):
    token = inventory = None

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
            return self.reply(200, {"hosts": run_job(body.get("capability"), body.get("devices"), self.inventory)})
        except (ValueError, AttributeError) as exc:
            return self.reply(400, {"error": str(exc)})
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            return self.reply(502, {"error": str(exc)})


def serve():
    Handler.token = os.environ["ORCH_ANSIBLE_TOKEN"]
    Handler.inventory = os.environ["ORCH_ANSIBLE_INVENTORY"]
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
