"""Governance: freeze periods, break-glass, time and daily budgets, signed anchors, the trajectory and change
evidence, CEF, and directory sign-in."""
import base64
import hashlib
import json
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from orchestrator import anchors, changes, db, loop, model, oidc, plugins, reports, siem
from tests.test_changes import DIGEST, acme, change_cap, propose  # noqa: F401  (acme is a fixture)
from tests.test_core import audit_kinds, scripted, set_policy, tool_call
from tests.test_web import call, sign_in, site  # noqa: F401  (site is a fixture)


def planned(tenant_id, critical=False, risk="medium"):
    if critical:
        with db.tenant(tenant_id) as conn:
            conn.execute("UPDATE devices SET critical = true")
    caps = change_cap([], risk=risk)
    return propose(tenant_id, "oscar", caps)["untrusted_tool_output"]["result"]["change"], caps


def test_nothing_runs_in_a_freeze_and_break_glass_does_not_lift_it(acme):
    change, caps = planned(acme)
    changes.decide(acme, "ava", change, DIGEST, True)
    now = datetime.now(timezone.utc)
    set_policy(acme, f"freeze: [{{start: '{(now - timedelta(hours=1)):%Y-%m-%dT%H:%M}', "
                     f"end: '{(now + timedelta(hours=1)):%Y-%m-%dT%H:%M}', reason: Year end}}]")
    with pytest.raises(plugins.Refused, match="frozen: Year end"):
        changes.run(acme, "oscar", change, caps)
    set_policy(acme, "{}")
    assert changes.run(acme, "oscar", change, caps)[0] == "VERIFIED"


def test_break_glass_is_one_administrator_with_a_reason_outside_the_window(acme):
    change, caps = planned(acme, critical=True)  # high risk: two approvers normally
    with pytest.raises(ValueError):
        changes.break_glass(acme, "alice", change, DIGEST, "urgent")
    with pytest.raises(PermissionError):
        changes.break_glass(acme, "ava", change, DIGEST, "core is down, need this now")
    with pytest.raises(plugins.Refused, match="not the plan"):
        changes.break_glass(acme, "alice", change, "e" * 64, "core is down, need this now")
    assert changes.break_glass(acme, "alice", change, DIGEST, "core is down, need this now") == "APPROVED"

    set_policy(acme, "change_window: {days: [mon], start: '00:00', end: '00:01'}")
    assert changes.run(acme, "oscar", change, caps)[0] == "VERIFIED"  # the window does not hold it back
    assert "change.break_glass" in audit_kinds(acme)
    with db.tenant(acme) as conn:
        card = changes.listing(conn, change)[0]
    assert card["break_glass"] == "core is down, need this now"
    assert card["decisions"][0]["reason"].startswith("BREAK-GLASS")

    destructive, _ = planned(acme, risk="destructive")
    with pytest.raises(plugins.Refused, match="destructive"):
        changes.break_glass(acme, "alice", destructive, DIGEST, "core is down, need this now")
    own = propose(acme, "alice", caps)["untrusted_tool_output"]["result"]["change"]
    with pytest.raises(PermissionError, match="someone else"):
        changes.break_glass(acme, "alice", own, DIGEST, "core is down, need this now")


def test_a_request_has_a_time_limit_and_a_tenant_a_daily_token_limit(acme):
    set_policy(acme, "limits: {max_seconds: 1, max_tokens_per_day: 100}")

    def slow(messages, tools):
        time.sleep(1.1)
        return {**tool_call("inventory__devices__list", {}), "usage": {"total_tokens": 150}}
    assert loop.run(acme, "oscar", "list", chat=slow)[1:] == ("FAILED", "no answer within 1 seconds")
    _, state, answer = loop.run(acme, "ava", "list", chat=scripted())
    assert state == "REFUSED" and "100 model tokens" in answer


def test_signed_anchors_catch_a_rewrite_of_the_ledger(acme, database, tmp_path):
    key = ed25519.Ed25519PrivateKey.generate()
    priv, pub = tmp_path / "anchor.key", tmp_path / "anchor.pub"
    priv.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    pub.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM,
                                                  serialization.PublicFormat.SubjectPublicKeyInfo))
    with db.tenant(acme) as conn:
        db.audit(conn, "alice", "test", {"n": 1})
    assert anchors.anchor_all(str(priv)) == 1
    assert anchors.anchor_all(str(priv)) == 0  # nothing new since
    with db.tenant(acme) as conn:
        db.audit(conn, "alice", "test", {"n": 2})
    assert anchors.anchor_all(str(priv)) == 0  # anchored within the hour
    assert anchors.anchor_all(str(priv), every=timedelta(0)) == 1
    with db.tenant(acme) as conn:
        assert anchors.verify(conn, acme, str(pub)) == (2, None)
        first = conn.execute("SELECT data FROM audit WHERE kind = 'audit.anchor' ORDER BY seq").fetchone()["data"]

    with psycopg.connect(database, autocommit=True) as conn:  # the owner rewrites the anchored row's hash
        conn.execute("ALTER TABLE audit DISABLE TRIGGER audit_append_only")
        conn.execute("UPDATE audit SET hash = repeat('0', 64) WHERE seq = %s", (first["seq"],))
        conn.execute("ALTER TABLE audit ENABLE TRIGGER audit_append_only")
    with db.tenant(acme) as conn:
        assert anchors.verify(conn, acme, str(pub))[1] is not None


def test_the_trajectory_and_the_evidence_come_from_the_ledger(acme):
    change, caps = planned(acme)
    changes.decide(acme, "ava", change, DIGEST, True)
    changes.run(acme, "oscar", change, caps)
    with db.tenant(acme) as conn:
        proof = reports.evidence(conn, {"user": "ava", "role": "approver"}, change)
        with pytest.raises(PermissionError):
            reports.evidence(conn, {"user": "vic", "role": "viewer"}, change)
        with pytest.raises(LookupError):
            reports.evidence(conn, {"user": "ava", "role": "approver"}, "00000000-0000-0000-0000-000000000000")
    assert [r["kind"] for r in proof["ledger"]] == ["change.planned", "change.approved", "change.executing",
                                                    "change.verified"]
    assert proof["chain"]["holds"] and proof["change"]["state"] == "VERIFIED"
    assert "request.received" in [s["kind"] for s in proof["request"]["steps"]]


def test_the_page_shows_steps_and_exports_evidence(site, monkeypatch):
    token = sign_in(site)[1]
    monkeypatch.setattr(model, "chat", scripted(tool_call("inventory__devices__list", {}), {"content": "core-sw1"}))
    request = call(site, "/api/ask", {"prompt": "list devices"}, cookie=token)[1]["request"]
    steps = call(site, f"/api/requests/{request}", cookie=token)[1]["steps"]
    assert [s["kind"] for s in steps][:2] == ["request.received", "request.state"]
    assert call(site, "/api/requests/00000000-0000-0000-0000-000000000000", cookie=token)[0] == 404
    assert call(site, f"/api/requests/{request}")[0] == 401


def test_cef_lines_escape_what_cef_reserves():
    row = {"at": datetime(2026, 10, 25, tzinfo=timezone.utc), "actor": "a=b", "tenant_id": "t", "seq": 7,
           "hash": "h", "kind": "kill_switch.stop|x", "data": {"reason": "line1\nline2 a=b"}}
    text = siem.cef(row)
    assert text.startswith("CEF:0|Rezztor|Orchestrator|1|kill_switch.stop\\|x|kill_switch.stop\\|x|7|rt=")
    assert "suser=a\\=b" in text and "\n" not in text
    assert siem.line(row, "cef").startswith(b"<110>1 2026-10-25T00:00:00.000Z")


class Directory(BaseHTTPRequestHandler):
    """A directory with discovery, keys and a token endpoint that hands out whatever ID token the test set."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    issued, forms = {}, []

    def answer(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        if self.path == "/.well-known/openid-configuration":
            return self.answer({"issuer": base, "authorization_endpoint": base + "/authorize",
                                "token_endpoint": base + "/token", "jwks_uri": base + "/jwks"})
        n = self.key.public_key().public_numbers()
        self.answer({"keys": [{"kty": "RSA", "kid": "k1", "n": b64(n.n.to_bytes(256)), "e": b64(n.e.to_bytes(3))}]})

    def do_POST(self):
        self.forms.append(dict(urllib.parse.parse_qsl(self.rfile.read(int(self.headers["Content-Length"])).decode())))
        self.answer({"id_token": self.issued["id_token"]})

    def log_message(self, *args):
        pass


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def id_token(claims, key=Directory.key):
    signed = f"{b64(json.dumps({'alg': 'RS256', 'kid': 'k1'}).encode())}.{b64(json.dumps(claims).encode())}"
    return f"{signed}.{b64(key.sign(signed.encode(), padding.PKCS1v15(), hashes.SHA256()))}"


def get(url, path, cookie=None):
    host = urllib.parse.urlsplit(url)
    conn = HTTPConnection(host.hostname, host.port)
    conn.request("GET", path, headers={"Cookie": cookie} if cookie else {})
    resp = conn.getresponse()
    return resp.status, resp.getheader("Location"), resp.msg.get_all("Set-Cookie") or [], resp.read().decode()


def test_directory_sign_in_proves_who_and_the_role_stays_the_tenants(site, monkeypatch):
    directory = ThreadingHTTPServer(("127.0.0.1", 0), Directory)
    threading.Thread(target=directory.serve_forever, daemon=True).start()
    issuer = f"http://127.0.0.1:{directory.server_port}"
    monkeypatch.setattr(oidc, "_config", {})
    monkeypatch.setenv("ORCH_PUBLIC_URL", "https://orch.example")
    monkeypatch.setenv("ACME_OIDC_SECRET", "s3cret")
    with db.app() as conn:
        tenant = conn.execute("SELECT tenant_id_by_name('acme') AS id").fetchone()["id"]
    with db.tenant(tenant) as conn:  # set_config insists on https; the test directory is plain HTTP on 127.0.0.1
        conn.execute("INSERT INTO oidc (tenant_id, issuer, client_id, secret_env, username_claim) VALUES "
                     "(current_setting('app.tenant_id')::uuid, %s, 'orch', 'ACME_OIDC_SECRET', 'upn')", (issuer,))

    def begin():
        status, location, cookies, _ = get(site, "/oidc/start?tenant=acme")
        assert status == 302 and location.startswith(issuer + "/authorize?")
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(location).query))
        return query, cookies[0].split(";")[0]

    def claims(query, **extra):
        return {"iss": issuer, "aud": "orch", "exp": time.time() + 300, "iat": time.time(), "nonce": query["nonce"],
                "upn": "alice", **extra}

    query, browser = begin()
    Directory.issued["id_token"] = id_token(claims(query))
    callback = f"/oidc/callback?state={urllib.parse.quote(query['state'])}&code=c1"
    assert get(site, callback, "orch_oidc=someone-else")[0] == 400  # started in another browser
    query, browser = begin()
    Directory.issued["id_token"] = id_token(claims(query))
    callback = f"/oidc/callback?state={urllib.parse.quote(query['state'])}&code=c1"
    status, location, cookies, _ = get(site, callback, browser)
    assert (status, location) == (302, "/")
    form = Directory.forms[-1]
    assert form["client_secret"] == "s3cret" and query["code_challenge"] == b64(
        hashlib.sha256(form["code_verifier"].encode()).digest())
    session = next(c for c in cookies if c.startswith("orch_session=")).split(";")[0].split("=", 1)[1]
    assert call(site, "/api/me", cookie=session)[1]["mfa"] == "enroll"  # an administrator still gives a code
    assert get(site, callback, browser)[0] == 400  # a sign-in is used once

    for bad in ({"aud": "someone-else"}, {"nonce": "replayed"}, {"upn": "mallory"}, {"exp": time.time() - 3600}):
        query, browser = begin()
        Directory.issued["id_token"] = id_token(claims(query, **bad))
        assert get(site, f"/oidc/callback?state={urllib.parse.quote(query['state'])}&code=c", browser)[0] == 400
    query, browser = begin()
    Directory.issued["id_token"] = id_token(claims(query), rsa.generate_private_key(public_exponent=65537,
                                                                                    key_size=2048))
    status, _, _, text = get(site, f"/oidc/callback?state={urllib.parse.quote(query['state'])}&code=c", browser)
    assert status == 400 and "signature" in text
    assert get(site, "/oidc/start?tenant=nobody")[0] == 400
    directory.shutdown()
