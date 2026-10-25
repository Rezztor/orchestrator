"""Model gateway: one OpenAI-compatible Chat Completions call.

That one protocol reaches a local model (vLLM, llama.cpp, Ollama) and most hosted providers. Configured by
ORCH_MODEL_URL (base URL ending in /v1), ORCH_MODEL and, for a hosted provider, ORCH_MODEL_KEY.
"""
import json
import os
import urllib.request

# shortcut: one endpoint per installation; route per tenant when tenants need different models.


def chat(messages, tools):
    """Send the conversation, return the assistant message dict ({"content", "tool_calls"?, "usage"})."""
    body = {"model": os.environ["ORCH_MODEL"], "messages": messages}
    if tools:
        body["tools"] = tools
    headers = {"Content-Type": "application/json"}
    if os.environ.get("ORCH_MODEL_KEY"):
        headers["Authorization"] = "Bearer " + os.environ["ORCH_MODEL_KEY"]
    req = urllib.request.Request(os.environ["ORCH_MODEL_URL"].rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.load(resp)
    message = body["choices"][0]["message"]
    message["usage"] = body.get("usage") or {}  # read by the loop's token budget, never sent back
    return message
