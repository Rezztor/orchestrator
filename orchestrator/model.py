"""Model gateway: one OpenAI-compatible Chat Completions call.

That one protocol reaches a local model (vLLM, llama.cpp, Ollama) and most hosted providers. Configured by
ORCH_MODEL_URL (base URL ending in /v1), ORCH_MODEL and, for a hosted provider, ORCH_MODEL_KEY. The change reviewer
uses ORCH_REVIEW_MODEL on the same endpoint when it is set: a second model catches what the first one misses.
A tenant with its own model route (route-set TENANT model URL KEY_ENV MODEL, for example a local model for device
data that must stay inside) uses that endpoint and model for everything, the reviewer included.
"""
import json
import os
import ssl
import urllib.request

from . import plugins


def chat(messages, tools, model=None):
    """Send the conversation, return the assistant message dict ({"content", "tool_calls"?, "usage"})."""
    own = plugins.route("model")
    if own:
        url, key, model, ca = own["url"], own["secret_env"] and os.environ[own["secret_env"]], own["model"], own["ca"]
    else:
        url, key, model, ca = (os.environ["ORCH_MODEL_URL"], os.environ.get("ORCH_MODEL_KEY"),
                               model or os.environ["ORCH_MODEL"], None)
    body = {"model": model, "messages": messages}
    if tools:
        body["tools"] = tools
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=120, context=ssl.create_default_context(cafile=ca)) as resp:
        body = json.load(resp)
    message = body["choices"][0]["message"]
    message["usage"] = body.get("usage") or {}  # read by the loop's token budget, never sent back
    return message
