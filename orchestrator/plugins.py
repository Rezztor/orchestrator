"""Plugin host: reads each plugin's manifest and runs only the capabilities it declares.

A plugin is a folder under plugins/ with plugin.yaml and plugin.py. The manifest is the contract: a capability that
is not in it does not exist, and its `params` schema is checked before the handler runs. A change capability's
`handler` is its dry run, returning {"digest", "rollback", "targets", "hosts"}; its `apply` handler takes the
approved digest and the same arguments (see changes.py).
"""
import importlib.util
import json
import os
import ssl
import urllib.error
import urllib.request
from pathlib import Path

import yaml

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"
TYPES = {"string": str, "integer": int, "boolean": bool, "array": list, "object": dict}

# ponytail: in-process plugins shipped in this tree only; signed out-of-process plugins (MCP) come with releases.


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


def post_job(prefix, body, timeout):
    """POST a job to an executor configured by PREFIX_URL, PREFIX_TOKEN and optional PREFIX_CA. Returns its JSON."""
    req = urllib.request.Request(
        os.environ[f"{prefix}_URL"].rstrip("/") + "/v1/jobs", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ[f"{prefix}_TOKEN"]})
    ctx = ssl.create_default_context(cafile=os.environ.get(f"{prefix}_CA"))
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
