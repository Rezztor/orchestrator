"""The web interface: sign-in and chat, one page and a small JSON API.

    ORCH_WEB_LISTEN      host:port, default 127.0.0.1:8080
    ORCH_WEB_CERT/KEY    serve HTTPS with this certificate and key (otherwise put TLS in front)

python -m orchestrator.web
"""
import json
import os
import re
import ssl
import threading
import traceback
import uuid
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import psycopg

from . import admin, auth, changes, db, loop, notify, oidc, plugins, policy, recipes, reports, skills

STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript"),
          "/app.css": ("app.css", "text/css")}
# GET pages of data: path -> (roles that may read it or None for everyone signed in, reader(conn, me, query))
READS = {
    "/api/requests": (None, lambda conn, me, q: {"requests": admin.history(conn, me)}),
    "/api/overview": (None, lambda conn, me, q: admin.overview(conn)),
    "/api/devices": (None, lambda conn, me, q: {"devices": admin.devices(conn)}),
    "/api/policy": (None, lambda conn, me, q: admin.policy_view(conn)),
    "/api/audit": (changes.APPROVER_ROLES, lambda conn, me, q: admin.ledger_page(
        conn, int(q["before"]) if q.get("before", "").isdigit() else None,
        q.get("kind") or None, q.get("actor") or None, q.get("ref") or None)),
    "/api/people": (("tenant-admin",), lambda conn, me, q: admin.people(conn)),
    "/api/connections": (("tenant-admin",), lambda conn, me, q: admin.connections(conn, me["tenant_id"])),
}
COOKIE = "orch_session"
ROUTE = re.compile(r"/api/(requests|changes/evidence)/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
HEADERS = {"Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; form-action 'self'",
           "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
           "Strict-Transport-Security": "max-age=31536000"}  # browsers ignore it over plain http (localhost)


class Handler(BaseHTTPRequestHandler):
    timeout = 60  # seconds a connection may sit idle: a client that sends slowly cannot hold a thread for ever

    def version_string(self):  # the Server header, without the Python version
        return "Rezztor-Orchestrator"

    def send(self, code, body, ctype="application/json", cookie=None, headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in {**HEADERS, **(headers or {})}.items():
            self.send_header(k, v)
        if cookie is not None:
            self.send_header("Set-Cookie", f"{COOKIE}={cookie}; Path=/; HttpOnly; Secure; SameSite=Strict"
                             + ("" if cookie else "; Max-Age=0"))
        self.end_headers()
        self.wfile.write(data)

    def token(self):
        morsel = SimpleCookie(self.headers.get("Cookie", "")).get(COOKIE)
        return morsel.value if morsel else None

    def me(self):
        """The signed-in user, or None; a session still waiting for its authenticator code counts as None."""
        me = auth.session(self.token())
        return me if me and me["mfa_ok"] else None

    def redirect(self, location, cookies=()):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for k, v in HEADERS.items():
            self.send_header(k, v)
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def directory_sign_in(self, url):
        """GET /oidc/start?tenant=NAME and the directory's GET /oidc/callback?state=...&code=..."""
        query = dict(parse_qsl(url.query))
        try:
            if url.path == "/oidc/start":
                location, browser = oidc.start(query.get("tenant", ""))
                # Lax, not Strict: the directory's redirect back is a cross-site navigation that must carry it
                return self.redirect(location, [f"orch_oidc={browser}; Path=/oidc; HttpOnly; Secure; SameSite=Lax; "
                                                "Max-Age=600"])
            if "error" in query:
                raise PermissionError(f"the directory refused: {query.get('error_description') or query['error']}")
            morsel = SimpleCookie(self.headers.get("Cookie", "")).get("orch_oidc")
            _, token = oidc.finish(query.get("state"), query.get("code", ""), morsel and morsel.value)
            return self.redirect("/", [f"{COOKIE}={token}; Path=/; HttpOnly; Secure; SameSite=Strict",
                                       "orch_oidc=; Path=/oidc; HttpOnly; Secure; SameSite=Lax; Max-Age=0"])
        except (LookupError, PermissionError) as exc:
            return self.send(400, f"Directory sign-in did not work: {exc}\n".encode(), "text/plain; charset=utf-8")
        except Exception:  # the directory is unreachable or answered nonsense; the reason goes to the log
            self.log_error("directory sign-in failed: %r", traceback.format_exc())
            return self.send(502, b"Directory sign-in did not work: the directory could not be reached.\n",
                             "text/plain; charset=utf-8")

    def do_GET(self):
        self.safely(self.get)

    def do_POST(self):
        self.safely(self.post)

    def safely(self, work):
        """Answer 500 to anything unexpected, with the reason in the log, rather than dropping the connection."""
        try:
            work()
        except Exception:
            self.log_error("request failed: %r", traceback.format_exc())
            self.send(500, {"error": "something went wrong; the reason is in the server log"})

    def get(self):
        url = urlsplit(self.path)
        if url.path in ("/oidc/start", "/oidc/callback"):
            return self.directory_sign_in(url)
        if self.path == "/healthz":  # for a load balancer: this process answers and reaches the database
            try:
                with db.app() as conn:
                    conn.execute("SELECT 1")
            except psycopg.Error:
                return self.send(503, {"ok": False})
            return self.send(200, {"ok": True})
        if self.path in STATIC:
            name, ctype = STATIC[self.path]
            return self.send(200, (Path(__file__).parent / "static" / name).read_bytes(), ctype)
        if self.path == "/api/me":
            me = auth.session(self.token())
            if not me:
                return self.send(401, {"error": "sign in"})
            if not me["mfa_ok"]:
                return self.send(200, {"user": me["user"], "role": me["role"], **auth.mfa_step(me)})
            with db.tenant(me["tenant_id"]) as conn:
                return self.send(200, {**me, "stopped": policy.stopped(conn)})
        if self.path == "/api/changes":
            me = self.me()
            if not me:
                return self.send(401, {"error": "sign in"})
            with db.tenant(me["tenant_id"]) as conn:
                return self.send(200, {"changes": changes.listing(conn)})
        if match := ROUTE.fullmatch(self.path):
            me = self.me()
            if not me:
                return self.send(401, {"error": "sign in"})
            kind, item = match.group(1), uuid.UUID(match.group(2))
            try:
                with db.tenant(me["tenant_id"]) as conn:
                    if kind == "requests":
                        return self.send(200, reports.trajectory(conn, me, item))
                    return self.send(200, json.dumps(reports.evidence(conn, me, item), default=str, indent=1).encode(),
                                     headers={"Content-Disposition": f'attachment; filename="change-{item}.json"'})
            except PermissionError as exc:
                return self.send(403, {"error": str(exc)})
            except LookupError as exc:
                return self.send(404, {"error": str(exc)})
        if url.path in READS:
            roles, read = READS[url.path]
            me = self.me()
            if not me:
                return self.send(401, {"error": "sign in"})
            if roles and me["role"] not in roles:
                return self.send(403, {"error": "your role cannot see this page"})
            with db.tenant(me["tenant_id"]) as conn:
                return self.send(200, read(conn, me, dict(parse_qsl(url.query))))
        if self.path == "/api/library":
            me = self.me()
            if not me:
                return self.send(401, {"error": "sign in"})
            with db.tenant(me["tenant_id"]) as conn:
                return self.send(200, {"recipes": recipes.listing(conn), "skills": skills.listing(conn),
                                       "shipped_skills": sorted(skills.shipped())})
        self.send(404, {"error": "not found"})

    def post(self):
        # JSON only: a cross-site form cannot send it without a preflight, and the cookie is SameSite=Strict.
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.send(415, {"error": "send JSON"})
        length = self.headers.get("Content-Length", "")
        length = int(length) if length.isascii() and length.isdigit() else 0
        if not 0 < length <= 32768:
            return self.send(413, {"error": "body must be 1 byte to 32 KiB"})
        try:
            body = json.loads(self.rfile.read(length))
            assert isinstance(body, dict)
        except (ValueError, AssertionError):
            return self.send(400, {"error": "body must be a JSON object"})

        if self.path.startswith("/hooks/"):  # another system starts a recipe; the secret in the path is its key
            try:
                tenant_id, run_as, recipe, values = recipes.fire(self.path.removeprefix("/hooks/"), body)
            except LookupError:
                return self.send(404, {"error": "not found"})
            except ValueError as exc:
                return self.send(400, {"error": str(exc)})
            threading.Thread(target=recipes.run, args=(tenant_id, run_as, recipe, values), daemon=True).start()
            return self.send(202, {"accepted": True})
        if self.path == "/api/sign-in":
            fields = [body.get(k) for k in ("tenant", "user", "password")]
            if not all(isinstance(f, str) and f for f in fields):
                return self.send(400, {"error": "tenant, user and password are required"})
            signed_in = auth.sign_in(*fields)
            if not signed_in:
                return self.send(401, {"error": "Those details did not sign you in."})
            return self.send(200, {"ok": True}, cookie=signed_in[1])
        if self.path == "/api/mfa":
            if not auth.check_code(self.token(), body.get("code")):
                return self.send(401, {"error": "That code did not match. Use the newest code your app shows."})
            return self.send(200, {"ok": True})
        if self.path == "/api/sign-out":
            auth.sign_out(self.token())
            return self.send(200, {"ok": True}, cookie="")
        if self.path == "/api/ask":
            me = self.me()
            if not me:
                return self.send(401, {"error": "sign in"})
            prompt = body.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 8000:
                return self.send(400, {"error": "a prompt of 1 to 8000 characters is required"})
            request_id, state, answer = loop.run(me["tenant_id"], me["user"], prompt.strip())
            if state == "FAILED":  # the reason is in the audit ledger, not shown to the browser
                answer = f"This request could not be completed. Reference {request_id}."
            return self.send(200, {"request": request_id, "state": state, "answer": answer})
        if self.path == "/api/kill-switch":
            me = self.me()
            if not me:
                return self.send(401, {"error": "sign in"})
            if me["role"] != "tenant-admin":
                return self.send(403, {"error": "only a tenant administrator can stop or resume agent work"})
            reason = body.get("reason")
            if body.get("stop") is True and not (isinstance(reason, str) and reason.strip()):
                return self.send(400, {"error": "say why you are stopping agent work"})
            loop.set_kill_switch(me["tenant_id"], me["user"],
                                 reason.strip()[:500] if body.get("stop") is True else None)
            with db.tenant(me["tenant_id"]) as conn:
                return self.send(200, {"stopped": policy.stopped(conn)})
        if self.path.startswith(("/api/people/", "/api/devices/", "/api/policy/", "/api/webhooks/", "/api/triggers/")):
            me = self.me()
            if not me:
                return self.send(401, {"error": "sign in"})
            if me["role"] != "tenant-admin":
                return self.send(403, {"error": "only a tenant administrator can change this"})
            try:
                return self.send(200, self.administer(me, body) or {"ok": True})
            except PermissionError as exc:
                return self.send(403, {"error": str(exc)})
            except LookupError as exc:
                return self.send(404, {"error": str(exc)})
            except (ValueError, TypeError) as exc:
                return self.send(400, {"error": str(exc)})
        if self.path in ("/api/skills/decide", "/api/recipes/run"):
            me = self.me()
            if not me:
                return self.send(401, {"error": "sign in"})
            name = body.get("name")
            if not isinstance(name, str):
                return self.send(400, {"error": "name is required"})
            try:
                if self.path == "/api/skills/decide":
                    with db.tenant(me["tenant_id"]) as conn:
                        skills.decide(conn, {"name": me["user"], "role": me["role"]}, name, body.get("approve") is True)
                    return self.send(200, {"ok": True})
                request_id, state, answer = recipes.run(me["tenant_id"], me["user"], name, body.get("values") or {})
            except PermissionError as exc:
                return self.send(403, {"error": str(exc)})
            except LookupError as exc:
                return self.send(404, {"error": str(exc)})
            except ValueError as exc:
                return self.send(400, {"error": str(exc)})
            if state == "FAILED":
                answer = f"This request could not be completed. Reference {request_id}."
            return self.send(200, {"request": request_id, "state": state, "answer": answer})
        if self.path in ("/api/changes/decide", "/api/changes/run", "/api/changes/record", "/api/changes/break-glass"):
            me = self.me()
            if not me:
                return self.send(401, {"error": "sign in"})
            try:
                change_id = uuid.UUID(str(body.get("change")))
            except ValueError:
                return self.send(400, {"error": "change must be a change id"})
            try:
                if self.path == "/api/changes/record":
                    if not isinstance(body.get("number"), str):
                        return self.send(400, {"error": "number is required"})
                    changes.set_record(me["tenant_id"], me["user"], change_id, body["number"].strip().upper())
                    return self.send(200, {"ok": True})
                if self.path == "/api/changes/break-glass":
                    if not isinstance(body.get("digest"), str):
                        return self.send(400, {"error": "the plan's digest is required"})
                    return self.send(200, {"state": changes.break_glass(me["tenant_id"], me["user"], change_id,
                                                                         body["digest"], body.get("reason"))})
                if self.path == "/api/changes/run":
                    change = changes.start(me["tenant_id"], me["user"], change_id)
                    # the executor can take many minutes; the page polls /api/changes for the outcome
                    threading.Thread(target=changes.finish, args=(me["tenant_id"], change, plugins.load()),
                                     daemon=True).start()
                    return self.send(202, {"state": "EXECUTING"})
                approve, reason = body.get("approve"), body.get("reason")
                if not isinstance(approve, bool) or not isinstance(body.get("digest"), str):
                    return self.send(400, {"error": "approve (true or false) and the plan's digest are required"})
                if not approve and not (isinstance(reason, str) and reason.strip()):
                    return self.send(400, {"error": "say why you are rejecting the change"})
                state = changes.decide(me["tenant_id"], me["user"], change_id, body["digest"], approve,
                                       reason.strip()[:500] if isinstance(reason, str) and reason.strip() else None)
                return self.send(200, {"state": state})
            except PermissionError as exc:
                return self.send(403, {"error": str(exc)})
            except LookupError as exc:
                return self.send(404, {"error": str(exc)})
            except plugins.Refused as exc:
                return self.send(409, {"error": str(exc)})
            except ValueError as exc:
                return self.send(400, {"error": str(exc)})
        self.send(404, {"error": "not found"})

    def administer(self, me, body):
        """A tenant administrator's change from the People, Devices, Policy or Integrations screens."""
        tenant, actor, path = me["tenant_id"], me["user"], self.path
        text = lambda key: body.get(key) if isinstance(body.get(key), str) else ""  # noqa: E731
        if path == "/api/people/add":
            password = body.get("password") or None
            return admin.add_person(tenant, actor, text("name").strip(), text("role"),
                                    password if isinstance(password, str) else None)
        if path == "/api/people/role":
            return admin.set_role(tenant, actor, text("name"), text("role"))
        if path == "/api/people/mfa-reset":
            return auth.reset_mfa(tenant, text("name"), actor)
        if path == "/api/webhooks/add":
            return notify.add(tenant, actor, text("kind"), text("url").strip())
        if path == "/api/triggers/add":
            address = recipes.add_trigger(tenant, actor, text("recipe"), text("run_as"))
            return {"address": os.environ.get("ORCH_PUBLIC_URL", "").rstrip("/") + address}
        with db.tenant(tenant) as conn:
            if path == "/api/devices/add":
                return admin.add_device(conn, actor, text("name"), text("address"), text("platform"), text("site"))
            if path == "/api/devices/critical":
                if not isinstance(body.get("critical"), bool):
                    raise ValueError("critical is true or false")
                return admin.set_critical(conn, actor, text("name"), body["critical"])
            if path == "/api/policy/set":
                return admin.set_policy(conn, actor, text("document"))
            if path == "/api/webhooks/remove":
                return admin.remove_webhook(conn, actor, uuid.UUID(text("id")))
            if path == "/api/triggers/remove":
                return admin.remove_trigger(conn, actor, text("id"))
        raise LookupError("no such setting")


def serve():
    host, port = os.environ.get("ORCH_WEB_LISTEN", "127.0.0.1:8080").rsplit(":", 1)
    server = ThreadingHTTPServer((host, int(port)), Handler)
    if os.environ.get("ORCH_WEB_CERT"):
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(os.environ["ORCH_WEB_CERT"], os.environ["ORCH_WEB_KEY"])
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    serve()
