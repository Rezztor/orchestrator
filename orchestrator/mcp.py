"""A client for the customer's own MCP servers (Model Context Protocol, Streamable HTTP), written from the
specification. Read-only by design: an MCP tool has no dry run or digest, so it can never be a change capability.

Each server is a manifest in ORCH_MCP_PLUGINS (a folder of NAME.yaml files the installation administrator reviews):

    name: netbox
    url_env: NETBOX_MCP_URL          # the server's MCP endpoint, from the environment
    token_env: NETBOX_MCP_TOKEN      # optional bearer token
    tenants: [<tenant id>, ...]      # who may use it; a server is never shared by accident
    capabilities:
      - id: mcp.netbox.device.get    # always mcp.NAME.<...>: roles allow mcp.* and a tenant can narrow it
        tool: get_device             # the server's tool name
        risk: low
        description: Look up one device in NetBox by name.
        params: {type: object, properties: {name: {type: string}}, required: [name], additionalProperties: false}

The parameters are the reviewed manifest's, never the server's own description, so a server cannot widen them.
"""
import json
import os
import re
import ssl
import urllib.error
import urllib.request
from functools import partial
from pathlib import Path

import yaml

from .plugins import Refused, setting

PROTOCOL = "2025-06-18"
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
MOST_ITEMS, MOST_TEXT = 50, 8000


def call_tool(url, token, tool, arguments, timeout=60):
    """initialize, notifications/initialized, tools/call; returns {"content": [text, ...], "structured"?}."""
    session = None

    def rpc(method, params, id_=None):
        nonlocal session
        body = {"jsonrpc": "2.0", "method": method, "params": params, **({"id": id_} if id_ else {})}
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": PROTOCOL}
        if token:
            headers["Authorization"] = "Bearer " + token
        if session:
            headers["Mcp-Session-Id"] = session
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context(
                    cafile=os.environ.get("ORCH_MCP_CA"))) as resp:
                session = resp.headers.get("Mcp-Session-Id") or session
                text, ctype = resp.read(4_000_000).decode(), resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            raise Refused(f"the MCP server answered {exc.code}")
        if id_ is None:
            return None
        if ctype.startswith("text/event-stream"):
            events = [json.loads(line[5:]) for line in text.splitlines() if line.startswith("data:")]
            msg = next((e for e in events if e.get("id") == id_), None)
        else:
            msg = json.loads(text)
        if not isinstance(msg, dict) or "error" in msg or "result" not in msg:
            raise Refused(f"the MCP server refused: {(msg or {}).get('error', {}).get('message', 'no answer')}")
        return msg["result"]

    rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                       "clientInfo": {"name": "rezztor-orchestrator", "version": "1"}}, 1)
    rpc("notifications/initialized", {})
    result = rpc("tools/call", {"name": tool, "arguments": arguments}, 2)
    texts = [c.get("text", "")[:MOST_TEXT] for c in result.get("content", [])[:MOST_ITEMS] if c.get("type") == "text"]
    if result.get("isError"):
        raise Refused("the MCP tool failed: " + " ".join(texts)[:500])
    return {"content": texts, **({"structured": result["structuredContent"]} if "structuredContent" in result else {})}


def handler(spec, tool, conn, **args):
    return call_tool(setting(spec["url_env"]), spec.get("token_env") and setting(spec["token_env"]), tool, args)


def load(folder):
    """{capability id: capability} from the manifests in `folder`. Raises ValueError on any manifest that breaks
    the rules, so a mistake stops the start-up instead of quietly widening anything."""
    caps = {}
    for path in sorted(Path(folder).glob("*.yaml")):
        spec = yaml.safe_load(path.read_text())
        if not (isinstance(spec, dict) and NAME.fullmatch(str(spec.get("name"))) and spec.get("url_env")
                and isinstance(spec.get("tenants"), list) and spec["tenants"]):
            raise ValueError(f"{path}: an MCP manifest has name, url_env, tenants and capabilities")
        for cap in spec.get("capabilities") or []:
            if not (str(cap.get("id", "")).startswith(f"mcp.{spec['name']}.") and cap.get("tool")
                    and cap.get("kind", "read") == "read" and isinstance(cap.get("params"), dict)):
                raise ValueError(f"{path}: {cap.get('id')} must be mcp.{spec['name']}.<...>, read only, with a tool "
                                 "and params")
            caps[cap["id"]] = {**cap, "kind": "read", "risk": cap.get("risk", "low"), "plugin": f"mcp:{spec['name']}",
                               "tenants": [str(t) for t in spec["tenants"]],
                               "handler": partial(handler, spec, cap["tool"])}
    return caps
