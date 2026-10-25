import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from orchestrator import auth, db, model, web
from tests.test_core import add_device, scripted, tool_call


@pytest.fixture
def site(database, monkeypatch):
    t = db.create_tenant("acme", "alice")
    auth.set_password(t, "alice", "correct horse battery")
    add_device(t, "core-sw1", "colombo")
    monkeypatch.setattr(auth, "_failures", {})
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def call(url, path, body=None, cookie=None, ctype="application/json"):
    headers = {"Content-Type": ctype} if body is not None else {}
    if cookie:
        headers["Cookie"] = f"orch_session={cookie}"
    req = urllib.request.Request(url + path, data=None if body is None else json.dumps(body).encode(),
                                 headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.load(resp), resp.headers
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc), exc.headers


def sign_in(url, password="correct horse battery"):
    status, _, headers = call(url, "/api/sign-in", {"tenant": "acme", "user": "alice", "password": password})
    cookie = headers.get("Set-Cookie", "")
    return status, cookie.split(";")[0].split("=", 1)[1] if status == 200 else None, cookie


def test_sign_in_ask_and_sign_out(site, monkeypatch):
    status, token, cookie = sign_in(site)
    assert status == 200 and "HttpOnly" in cookie and "Secure" in cookie and "SameSite=Strict" in cookie
    assert call(site, "/api/me", cookie=token)[1]["user"] == "alice"

    monkeypatch.setattr(model, "chat", scripted(tool_call("inventory__devices__list", {}), {"content": "core-sw1"}))
    status, body, _ = call(site, "/api/ask", {"prompt": "list devices"}, cookie=token)
    assert (status, body["state"], body["answer"]) == (200, "DONE", "core-sw1")

    call(site, "/api/sign-out", {}, cookie=token)
    assert call(site, "/api/me", cookie=token)[0] == 401
    assert call(site, "/api/ask", {"prompt": "list devices"}, cookie=token)[0] == 401


def test_wrong_passwords_lock_the_account_for_a_while(site):
    for _ in range(auth.MAX_FAILURES):
        assert sign_in(site, "wrong password!")[0] == 401
    assert sign_in(site)[0] == 401  # right password, but locked
    with db.app() as conn:
        acme = conn.execute("SELECT tenant_id_by_name('acme') AS id").fetchone()["id"]
    with db.tenant(acme) as conn:
        assert conn.execute("SELECT count(*) AS n FROM audit WHERE kind = 'auth.sign_in_failed'").fetchone()["n"] == 5


def test_requests_that_are_not_json_or_not_signed_in_are_refused(site):
    assert call(site, "/api/sign-in", {"tenant": "acme"}, ctype="text/plain")[0] == 415
    assert call(site, "/api/ask", {"prompt": "hi"})[0] == 401
    assert call(site, "/api/ask", {"prompt": "hi"}, cookie="forged-token")[0] == 401
    assert call(site, "/api/me")[0] == 401


def test_a_failed_request_shows_a_reference_not_the_error(site, monkeypatch):
    _, token, _ = sign_in(site)

    def broken(messages, tools):
        raise ConnectionError("postgres://secret@host refused")
    monkeypatch.setattr(model, "chat", broken)
    body = call(site, "/api/ask", {"prompt": "hi"}, cookie=token)[1]
    assert body["state"] == "FAILED" and "secret" not in body["answer"] and body["request"] in body["answer"]


def test_short_passwords_are_refused(database):
    t = db.create_tenant("acme", "alice")
    with pytest.raises(ValueError):
        auth.set_password(t, "alice", "short")


def test_only_a_tenant_admin_can_stop_agent_work(site):
    _, token, _ = sign_in(site)
    with db.app() as conn:
        acme = conn.execute("SELECT tenant_id_by_name('acme') AS id").fetchone()["id"]
    with db.tenant(acme) as conn:
        conn.execute("INSERT INTO users (tenant_id, name, role) VALUES (%s, 'olga', 'operator')", (acme,))
    auth.set_password(acme, "olga", "another good password")
    _, _, headers = call(site, "/api/sign-in", {"tenant": "acme", "user": "olga", "password": "another good password"})
    olga = headers["Set-Cookie"].split(";")[0].split("=", 1)[1]

    assert call(site, "/api/kill-switch", {"stop": True, "reason": "x"}, cookie=olga)[0] == 403
    assert call(site, "/api/kill-switch", {"stop": True}, cookie=token)[0] == 400
    assert call(site, "/api/kill-switch", {"stop": True, "reason": "change freeze"}, cookie=token)[1] == \
        {"stopped": "change freeze"}
    assert call(site, "/api/me", cookie=olga)[1]["stopped"] == "change freeze"
    assert call(site, "/api/ask", {"prompt": "list devices"}, cookie=olga)[1]["state"] == "REFUSED"
    assert call(site, "/api/kill-switch", {"stop": False}, cookie=token)[1] == {"stopped": None}
