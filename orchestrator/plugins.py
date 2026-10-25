"""Plugin host: reads each plugin's manifest and runs only the capabilities it declares.

A plugin is a folder under plugins/ with plugin.yaml and plugin.py. The manifest is the contract: a capability that
is not in it does not exist, and its `params` schema is checked before the handler runs.
"""
import importlib.util
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
            if cap["kind"] not in ("read", "change"):
                raise ValueError(f"{manifest}: {cap['id']} has kind {cap['kind']!r}")
            caps[cap["id"]] = {**cap, "plugin": spec["name"], "handler": getattr(module, cap["handler"])}
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
