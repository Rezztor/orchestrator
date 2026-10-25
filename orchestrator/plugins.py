"""Plugin host: reads each plugin's manifest and runs only the capabilities it declares.

A plugin is a folder under plugins/ with plugin.yaml and plugin.py. The manifest is the contract: a capability that
is not in it does not exist, and its `params` schema is checked before the handler runs. A change capability's
`handler` is its dry run, returning {"digest", "rollback", "targets", "hosts"}; its `apply` handler takes the
approved digest and the same arguments (see changes.py).
"""
import contextvars
import importlib.util
import json
import os
import ssl
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from . import db

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"
TYPES = {"string": str, "integer": int, "boolean": bool, "array": list, "object": dict}

TENANT = contextvars.ContextVar("tenant", default=None)  # set for a request or a run: whose executors and model
ROUTE_KINDS = ("ansible", "tofu", "model")


class Refused(Exception):
    """A call the host will not run. The message goes back to the model and into the audit."""


def load(plugin_dir=PLUGIN_DIR):
    """Return {capability id: capability dict with its handler}."""
    caps = {}
    for manifest in sorted(Path(plugin_dir).glob("*/plugin.yaml")):
        spec = yaml.safe_load(manifest.read_text())
        loader = importlib.util.spec_from_file_location(f"orch_plugin_{spec['name']}", manifest.with_name("plugin.py"))
        module = importlib.util.module_from_spec(loader)
        loader.loader.exec_module(module)
        for cap in spec["capabilities"]:
            if cap["kind"] not in ("read", "change") or (cap["kind"] == "change") != ("apply" in cap):
                raise ValueError(f"{manifest}: {cap['id']} must be a read, or a change with an apply handler")
            caps[cap["id"]] = {**cap, "plugin": spec["name"], "handler": getattr(module, cap["handler"]),
                               **({"apply": getattr(module, cap["apply"])} if "apply" in cap else {})}
    if os.environ.get("ORCH_MCP_PLUGINS"):  # the customer's own MCP servers, read only; see mcp.py
        from . import mcp
        extra = mcp.load(os.environ["ORCH_MCP_PLUGINS"])
        if clash := set(extra) & set(caps):
            raise ValueError(f"MCP manifests redefine {', '.join(sorted(clash))}")
        caps.update(extra)
    return caps


def tool_name(cap_id):
    return cap_id.replace(".", "__")


def tools(caps, offer=lambda cap: True):
    """The capabilities `offer` accepts, as Chat Completions tool definitions."""
    return [{"type": "function", "function": {"name": tool_name(c["id"]), "description": c["description"],
                                              "parameters": c["params"]}} for c in caps.values() if offer(c)]


def check_args(schema, args):
    if not isinstance(args, dict):
        raise Refused("arguments must be an object")
    props = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in args:
            raise Refused(f"missing argument {key!r}")
    for key, value in args.items():
        if key not in props:
            raise Refused(f"unknown argument {key!r}")
        want = TYPES[props[key]["type"]]
        if not isinstance(value, want) or (want is int and isinstance(value, bool)):
            raise Refused(f"argument {key!r} must be {props[key]['type']}")


def find(caps, name):
    cap = next((c for c in caps.values() if tool_name(c["id"]) == name), None)
    if cap is None:
        raise Refused(f"no capability {name!r}")
    return cap


def route(kind):
    """The current tenant's own endpoint for an executor kind or the model ({url, secret_env, ca, model}), or None
    when it uses the installation's (set with route-set; an executor bound to one tenant and its network)."""
    tenant = TENANT.get()
    if tenant is None:
        return None
    with db.tenant(tenant) as conn:
        return conn.execute("SELECT url, secret_env, ca, model FROM routes WHERE kind = %s", (kind,)).fetchone()


def set_route(tenant_id, actor, kind, url, secret_env, model=None, ca=None):
    if kind not in ROUTE_KINDS or not url.startswith(("https://", "http://")):
        raise ValueError(f"a route is one of {', '.join(ROUTE_KINDS)} with an http(s) URL")
    with db.tenant(tenant_id) as conn:
        conn.execute("INSERT INTO routes (tenant_id, kind, url, secret_env, model, ca) VALUES "
                     "(current_setting('app.tenant_id')::uuid, %s, %s, %s, %s, %s) ON CONFLICT (tenant_id, kind) "
                     "DO UPDATE SET url = EXCLUDED.url, secret_env = EXCLUDED.secret_env, model = EXCLUDED.model, "
                     "ca = EXCLUDED.ca", (kind, url, secret_env, model, ca))
        db.audit(conn, actor, "route.set", {"kind": kind, "url": url, "secret_env": secret_env, "model": model})


def post_job(prefix, body, timeout):
    """POST a job to the tenant's executor (route-set), else the installation's PREFIX_URL, PREFIX_TOKEN and optional
    PREFIX_CA. Returns its JSON."""
    own = route(prefix.removeprefix("ORCH_").lower())
    url, token, ca = ((own["url"], os.environ[own["secret_env"]], own["ca"]) if own else
                      (os.environ[f"{prefix}_URL"], os.environ[f"{prefix}_TOKEN"], os.environ.get(f"{prefix}_CA")))
    req = urllib.request.Request(
        url.rstrip("/") + "/v1/jobs", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    ctx = ssl.create_default_context(cafile=ca)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        raise Refused(f"executor answered {exc.code}: {exc.read()[:300].decode(errors='replace')}")


def known_devices(conn, devices, most=50):
    """Refuse unless every name is one of this tenant's devices: an executor only ever hears of the tenant's own."""
    if not 0 < len(devices) <= most:
        raise Refused(f"ask for 1 to {most} devices at a time")
    found = {r["name"] for r in conn.execute("SELECT name FROM devices WHERE name = ANY(%s::text[])", (devices,))}
    unknown = sorted(set(devices) - found)
    if unknown:
        raise Refused(f"not in this tenant's inventory: {', '.join(unknown)}")
