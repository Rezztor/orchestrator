import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from orchestrator import auth, db, model, plugins, recipes, skills, web
from tests.test_changes import DIGEST, change_cap
from tests.test_core import add_device, scripted, tool_call
from tests.test_policy_guard import add_user


@pytest.fixture
def site(database, monkeypatch):
    t = db.create_tenant("acme", "alice")
    auth.set_password(t, "alice", "correct horse battery")
    add_device(t, "core-sw1", "colombo")
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


def sign_in(url, password="correct horse battery", user="alice", mfa=True):
    """Sign in, and for an approver or administrator also enrol (or use) the authenticator, unless mfa=False."""
    status, _, headers = call(url, "/api/sign-in", {"tenant": "acme", "user": user, "password": password})
    cookie = headers.get("Set-Cookie", "")
    token = cookie.split(";")[0].split("=", 1)[1] if status == 200 else None
    step = token and call(url, "/api/me", cookie=token)[1]
    if mfa and step and step.get("mfa"):
        with db.app() as conn:
            tenant = conn.execute("SELECT tenant_id_by_name('acme') AS id").fetchone()["id"]
        with db.tenant(tenant) as conn:  # every test sign-in is a new code; real ones wait for the next 30 seconds
            conn.execute("UPDATE users SET totp_last_step = 0 WHERE name = %s", (user,))
            secret = conn.execute("SELECT coalesce(totp_secret, totp_pending) AS s FROM users WHERE name = %s",
                                  (user,)).fetchone()["s"]
        assert call(url, "/api/mfa", {"code": auth.totp(secret, int(time.time()) // 30)}, cookie=token)[0] == 200
    return status, token, cookie


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


def test_approvers_and_admins_need_an_authenticator_code(site):
    _, token, _ = sign_in(site, mfa=False)
    step = call(site, "/api/me", cookie=token)[1]
    assert step["mfa"] == "enroll" and step["uri"].startswith("otpauth://totp/") and "stopped" not in step
    assert call(site, "/api/changes", cookie=token)[0] == 401  # nothing but the code step is open
    assert call(site, "/api/mfa", {"code": "000000"}, cookie=token)[0] == 401
    code = auth.totp(step["secret"], int(time.time()) // 30)
    assert call(site, "/api/mfa", {"code": code}, cookie=token)[0] == 200
    assert call(site, "/api/changes", cookie=token)[0] == 200

    _, again, _ = sign_in(site, mfa=False)
    assert call(site, "/api/me", cookie=again)[1] == {"user": "alice", "role": "tenant-admin", "mfa": "code"}
    assert call(site, "/api/mfa", {"code": code}, cookie=again)[0] == 401  # a code counts once
    for _ in range(auth.MAX_FAILURES - 1):
        call(site, "/api/mfa", {"code": "123456"}, cookie=again)
    assert call(site, "/api/me", cookie=again)[0] == 401  # too many wrong codes end the session


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
    olga = sign_in(site, "another good password", "olga")[1]

    assert call(site, "/api/kill-switch", {"stop": True, "reason": "x"}, cookie=olga)[0] == 403
    assert call(site, "/api/kill-switch", {"stop": True}, cookie=token)[0] == 400
    assert call(site, "/api/kill-switch", {"stop": True, "reason": "change freeze"}, cookie=token)[1] == \
        {"stopped": "change freeze"}
    assert call(site, "/api/me", cookie=olga)[1]["stopped"] == "change freeze"
    assert call(site, "/api/ask", {"prompt": "list devices"}, cookie=olga)[1]["state"] == "REFUSED"
    assert call(site, "/api/kill-switch", {"stop": False}, cookie=token)[1] == {"stopped": None}


def test_people_approve_and_run_a_change_from_the_page(site, monkeypatch):
    applied = []
    caps = change_cap(applied)
    monkeypatch.setattr(plugins, "load", lambda: caps)
    with db.app() as conn:
        tenant = conn.execute("SELECT tenant_id_by_name('acme') AS id").fetchone()["id"]
    add_user(tenant, "ava", "approver")
    auth.set_password(tenant, "ava", "correct horse battery")
    alice = sign_in(site)[1]
    ava = sign_in(site, user="ava")[1]

    monkeypatch.setattr(model, "chat", scripted(tool_call("ansible__test__set", {"devices": ["core-sw1"]}),
                                                {"content": "Planned."}))
    call(site, "/api/ask", {"prompt": "set it"}, cookie=alice)
    change = call(site, "/api/changes", cookie=ava)[1]["changes"][0]
    assert (change["state"], change["requested_by"], change["digest"]) == ("PLANNED", "alice", DIGEST)

    decide = {"change": change["id"], "digest": DIGEST, "approve": True}
    assert call(site, "/api/changes/decide", decide, cookie=alice)[0] == 403  # alice asked for it
    assert call(site, "/api/changes/run", {"change": change["id"]}, cookie=alice)[0] == 409  # not approved yet
    assert call(site, "/api/changes/decide", {**decide, "approve": False}, cookie=ava)[0] == 400  # no reason given
    assert call(site, "/api/changes/decide", {**decide, "change": "x"}, cookie=ava)[0] == 400
    assert call(site, "/api/changes/decide", decide, cookie=ava)[1] == {"state": "APPROVED"}
    assert call(site, "/api/changes/run", {"change": change["id"]}, cookie=alice)[:2] == (202, {"state": "EXECUTING"})
    for _ in range(50):
        if call(site, "/api/changes", cookie=alice)[1]["changes"][0]["state"] != "EXECUTING":
            break
        time.sleep(0.1)
    assert call(site, "/api/changes", cookie=alice)[1]["changes"][0]["state"] == "VERIFIED" and len(applied) == 1


def test_recipes_run_and_only_an_admin_decides_on_skills(site, monkeypatch):
    with db.app() as conn:
        tenant = conn.execute("SELECT tenant_id_by_name('acme') AS id").fetchone()["id"]
    add_user(tenant, "oscar", "operator")
    auth.set_password(tenant, "oscar", "correct horse battery")
    recipes.add(tenant, "alice", "site-devices", "description: Devices at a site\nprompt: List devices at {site}.\n")
    with db.tenant(tenant) as conn:
        skills.add(conn, "agent", skills.check("vlan-audit", "Audit VLANs.", "1. List devices.", []), "induced",
                   "proposed")
    alice = sign_in(site)[1]
    oscar = sign_in(site, user="oscar")[1]

    library = call(site, "/api/library", cookie=oscar)[1]
    assert library["recipes"][0]["params"] == ["site"] and library["skills"][0]["state"] == "proposed"
    assert "login-banner" in library["shipped_skills"]
    monkeypatch.setattr(model, "chat", scripted({"content": "core-sw1"}))
    status, body, _ = call(site, "/api/recipes/run", {"name": "site-devices", "values": {"site": "colombo"}},
                           cookie=oscar)
    assert (status, body["state"], body["answer"]) == (200, "DONE", "core-sw1")
    assert call(site, "/api/recipes/run", {"name": "site-devices", "values": {}}, cookie=oscar)[0] == 400

    assert call(site, "/api/skills/decide", {"name": "vlan-audit", "approve": True}, cookie=oscar)[0] == 403
    assert call(site, "/api/skills/decide", {"name": "vlan-audit", "approve": True}, cookie=alice)[0] == 200
    assert call(site, "/api/library", cookie=oscar)[1]["skills"][0]["state"] == "approved"
