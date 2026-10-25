"""ChatOps: post change and kill-switch events to a tenant's Teams, Slack or JSON webhooks, with a link to the page.

People approve on the page, where they are signed in, never with a chat button: a chat message cannot prove who
pressed it. ORCH_PUBLIC_URL is the page's address for the links.
"""
import json
import logging
import os
import threading
import urllib.request

from . import db

KINDS = ("teams", "slack", "json")


def add(tenant_id, actor, kind, url):
    if kind not in KINDS or not url.startswith("https://"):
        raise ValueError(f"a webhook is one of {', '.join(KINDS)} with an https:// address")
    with db.tenant(tenant_id) as conn:
        conn.execute("INSERT INTO webhooks (tenant_id, kind, url) VALUES (current_setting('app.tenant_id')::uuid, "
                     "%s, %s)", (kind, url))
        db.audit(conn, actor, "webhook.added", {"kind": kind})  # the address carries a secret: not in the ledger


def payload(kind, event, text, link, data):
    if kind == "json":
        return {"event": event, "text": text, "link": link, **data}
    if kind == "slack":
        return {"text": f"{text}\n<{link}|Open the orchestrator>"}
    return {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": {
        "type": "AdaptiveCard", "version": "1.4", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "body": [{"type": "TextBlock", "text": text, "wrap": True}],
        "actions": [{"type": "Action.OpenUrl", "title": "Open the orchestrator", "url": link}]}}]}


def send(tenant_id, event, text, **data):
    """Post to every webhook of the tenant, in the background. Failures are audited, never raised."""
    with db.tenant(tenant_id) as conn:
        hooks = conn.execute("SELECT kind, url FROM webhooks").fetchall()
    if not hooks:
        return None
    link = os.environ.get("ORCH_PUBLIC_URL", "").rstrip("/") + "/" + (f"#change-{data['change']}" if "change" in data
                                                                       else "")

    def post_all():
        for hook in hooks:
            try:
                req = urllib.request.Request(hook["url"], data=json.dumps(payload(hook["kind"], event, text, link,
                                                                                    data)).encode(),
                                             headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=15).close()
            except Exception as exc:
                try:
                    with db.tenant(tenant_id) as conn:
                        db.audit(conn, "notify", "notify.failed", {"event": event, "kind": hook["kind"],
                                                                    "error": type(exc).__name__})
                except Exception:
                    logging.exception("could not audit a failed notification")
    thread = threading.Thread(target=post_all, daemon=True)
    thread.start()
    return thread
