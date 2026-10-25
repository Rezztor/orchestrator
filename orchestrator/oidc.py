"""Directory sign-in with OpenID Connect (Entra ID, Okta, Keycloak, Google and the like): authorization code with
PKCE, the ID token's signature checked against the directory's published keys, and its subject matched to a user
the tenant already has (by the claim the tenant names, preferred_username unless set). The directory proves who
someone is; their role stays the tenant's, and approvers and administrators still give an authenticator code.

    oidc-set TENANT_ID ISSUER CLIENT_ID SECRET_ENV [CLAIM]     the directory's issuer URL (https), the app's client
                                                              id, and the name of the environment variable holding
                                                              its client secret
    ORCH_PUBLIC_URL      the page's address; register ORCH_PUBLIC_URL/oidc/callback as the app's redirect URI
    ORCH_OIDC_CA         CA bundle for the directory, when it is not publicly trusted
"""
import base64
import hashlib
import json
import os
import secrets
import ssl
import time
import urllib.parse
import urllib.request

import psycopg
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from . import auth, db

LOGIN_MINUTES = 10
_config = {}  # issuer -> (fetched at, discovery document, keys)


def b64(data):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def b64e(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def fetch(url, form=None):
    """GET (or POST a form) over HTTPS and return the JSON answer."""
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode() if form else None,
                                 headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15, context=ssl.create_default_context(
            cafile=os.environ.get("ORCH_OIDC_CA"))) as resp:
        return json.load(resp)


def directory(issuer):
    """The issuer's discovery document and signing keys, kept for an hour."""
    at, doc, keys = _config.get(issuer, (0, None, None))
    if time.monotonic() - at > 3600:
        doc = fetch(issuer.rstrip("/") + "/.well-known/openid-configuration")
        if doc.get("issuer") != issuer:
            raise PermissionError("the directory's discovery document names another issuer")
        keys = {k.get("kid"): k for k in fetch(doc["jwks_uri"])["keys"]}
        _config[issuer] = (time.monotonic(), doc, keys)
    return doc, keys


def set_config(tenant_id, issuer, client_id, secret_env, claim="preferred_username"):
    if not issuer.startswith("https://"):
        raise ValueError("the issuer is an https:// URL")
    with db.tenant(tenant_id) as conn:
        conn.execute("INSERT INTO oidc (tenant_id, issuer, client_id, secret_env, username_claim) VALUES "
                     "(current_setting('app.tenant_id')::uuid, %s, %s, %s, %s) ON CONFLICT (tenant_id) DO UPDATE "
                     "SET issuer = EXCLUDED.issuer, client_id = EXCLUDED.client_id, secret_env = EXCLUDED.secret_env, "
                     "username_claim = EXCLUDED.username_claim", (issuer, client_id, secret_env, claim))
        db.audit(conn, "cli", "oidc.set", {"issuer": issuer, "client_id": client_id, "claim": claim})


def callback_url():
    return os.environ["ORCH_PUBLIC_URL"].rstrip("/") + "/oidc/callback"


def start(tenant_name):
    """Begin a directory sign-in. Returns (URL to send the browser to, browser cookie value)."""
    with db.app() as conn:
        tenant_id = conn.execute("SELECT tenant_id_by_name(%s) AS id", (tenant_name,)).fetchone()["id"]
    if tenant_id is None:
        raise LookupError("no such organisation, or it does not use directory sign-in")
    with db.tenant(tenant_id) as conn:
        cfg = conn.execute("SELECT * FROM oidc").fetchone()
        if cfg is None:
            raise LookupError("no such organisation, or it does not use directory sign-in")
        doc, _ = directory(cfg["issuer"])
        state, browser, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(4))
        conn.execute("DELETE FROM oidc_logins WHERE created_at < now() - make_interval(mins => %s)", (LOGIN_MINUTES,))
        conn.execute("INSERT INTO oidc_logins (state_hash, tenant_id, browser_hash, nonce, verifier) VALUES "
                     "(%s, current_setting('app.tenant_id')::uuid, %s, %s, %s)",
                     (auth.token_hash(state), auth.token_hash(browser), nonce, verifier))
    query = urllib.parse.urlencode({
        "response_type": "code", "client_id": cfg["client_id"], "redirect_uri": callback_url(),
        "scope": "openid profile email", "state": f"{tenant_id}.{state}", "nonce": nonce,
        "code_challenge": b64e(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256"})
    return f"{doc['authorization_endpoint']}?{query}", browser


def verify_id_token(token, keys, issuer, client_id, nonce):
    header, payload, signature = token.split(".")
    head, claims = json.loads(b64(header)), json.loads(b64(payload))
    key, signed, sig = keys.get(head.get("kid")), f"{header}.{payload}".encode(), b64(signature)
    if key is None:
        raise PermissionError("the ID token is signed with a key the directory does not publish")
    try:
        if head.get("alg") == "RS256" and key["kty"] == "RSA":
            rsa.RSAPublicNumbers(int.from_bytes(b64(key["e"])), int.from_bytes(b64(key["n"]))).public_key().verify(
                sig, signed, padding.PKCS1v15(), hashes.SHA256())
        elif head.get("alg") == "ES256" and key["kty"] == "EC" and key.get("crv") == "P-256":
            ec.EllipticCurvePublicNumbers(int.from_bytes(b64(key["x"])), int.from_bytes(b64(key["y"])),
                                          ec.SECP256R1()).public_key().verify(
                encode_dss_signature(int.from_bytes(sig[:32]), int.from_bytes(sig[32:])), signed,
                ec.ECDSA(hashes.SHA256()))
        else:
            raise PermissionError(f"the ID token uses {head.get('alg')!r}; RS256 and ES256 are accepted")
    except InvalidSignature:
        raise PermissionError("the ID token's signature does not hold")
    audience = claims.get("aud")
    now = time.time()
    if (claims.get("iss") != issuer or client_id not in (audience if isinstance(audience, list) else [audience])
            or not now < claims.get("exp", 0) + 60 or claims.get("iat", 0) > now + 60
            or not secrets.compare_digest(str(claims.get("nonce", "")), nonce)):
        raise PermissionError("the ID token is not for this sign-in")
    return claims


def finish(state, code, browser):
    """Complete a directory sign-in from the callback. Returns (tenant_id, session token); raises PermissionError."""
    tenant_id, _, state = (state or "").partition(".")
    try:
        with db.tenant(tenant_id) as conn:
            login = conn.execute("DELETE FROM oidc_logins WHERE state_hash = %s AND created_at > now() - "
                                 "make_interval(mins => %s) RETURNING *", (auth.token_hash(state), LOGIN_MINUTES)
                                 ).fetchone()
            cfg = conn.execute("SELECT * FROM oidc").fetchone()
    except psycopg.Error:  # a forged state names no tenant, or one that is not a UUID
        login = None
    if (login is None or cfg is None
            or not secrets.compare_digest(login["browser_hash"], auth.token_hash(browser or ""))):
        raise PermissionError("this sign-in link has expired or was started in another browser; start again")
    doc, keys = directory(cfg["issuer"])
    tokens = fetch(doc["token_endpoint"], {
        "grant_type": "authorization_code", "code": code, "redirect_uri": callback_url(),
        "client_id": cfg["client_id"], "client_secret": os.environ[cfg["secret_env"]],
        "code_verifier": login["verifier"]})
    claims = verify_id_token(tokens["id_token"], keys, cfg["issuer"], cfg["client_id"], login["nonce"])
    name = claims.get(cfg["username_claim"])
    with db.tenant(tenant_id) as conn:
        user = conn.execute("SELECT id, role FROM users WHERE name = %s", (name,)).fetchone()
        if user is None:
            db.audit(conn, str(name), "auth.sign_in_failed", {"method": "oidc", "reason": "no such user"})
            raise PermissionError(f"{name} is not a user of this organisation; ask its administrator to add you")
        return tenant_id, auth.new_session(conn, user, name, "oidc")
