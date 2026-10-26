"""Passwords, authenticator codes and sessions. Directory sign-in (OIDC) is in oidc.py and ends in a session here.

Approvers and tenant administrators need an authenticator code (TOTP, RFC 6238) after the password or the directory:
until then their session opens only the code step. The first time, the page offers a new secret to enrol.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

from . import db

SESSION_HOURS = 8
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
MAX_FAILURES, LOCKOUT_SECONDS = 5, 300
MFA_ROLES = ("approver", "tenant-admin")


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password, stored):
    if not stored:
        hash_password(password)  # same work as a real check, so timing does not reveal who has a password
        return False
    _, salt, digest = stored.split("$")
    return hmac.compare_digest(hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **SCRYPT).hex(), digest)


def set_password(tenant_id, user_name, password):
    if len(password) < 12:
        raise ValueError("a password needs at least 12 characters")
    with db.tenant(tenant_id) as conn:
        if conn.execute("UPDATE users SET password_hash = %s WHERE name = %s",
                        (hash_password(password), user_name)).rowcount != 1:
            raise LookupError(f"no user {user_name!r}")
        conn.execute("DELETE FROM sessions WHERE user_id = (SELECT id FROM users WHERE name = %s)", (user_name,))
        db.audit(conn, user_name, "auth.password_set")


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def sign_in(tenant_name, user_name, password):
    """Return (tenant_id, session token), or None. Every attempt on a real tenant is audited."""
    key = (tenant_name, user_name)
    with db.app() as conn:
        if conn.execute("SELECT 1 FROM sign_in_failures WHERE tenant_name = %s AND user_name = %s AND failures >= %s "
                        "AND last_at > now() - make_interval(secs => %s)", (*key, MAX_FAILURES, LOCKOUT_SECONDS)
                        ).fetchone():
            return None
        tenant_id = conn.execute("SELECT tenant_id_by_name(%s) AS id", (tenant_name,)).fetchone()["id"]
    if tenant_id is None:
        check_password(password, None)
        return _failed(key)
    with db.tenant(tenant_id) as conn:
        user = conn.execute("SELECT id, role, password_hash FROM users WHERE name = %s", (user_name,)).fetchone()
        if not check_password(password, user and user["password_hash"]):
            db.audit(conn, user_name, "auth.sign_in_failed")
            return _failed(key)
        token = new_session(conn, user, user_name, "password")
    with db.app() as conn:
        conn.execute("DELETE FROM sign_in_failures WHERE tenant_name = %s AND user_name = %s", key)
    return tenant_id, token


def new_session(conn, user, user_name, method):
    """Open a session for a user whose identity was just proved (in this tenant's transaction). Returns its token."""
    conn.execute("DELETE FROM sessions WHERE expires_at < now()")
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO sessions (token_hash, tenant_id, user_id, expires_at, mfa_ok) VALUES "
                 "(%s, current_setting('app.tenant_id')::uuid, %s, now() + make_interval(hours => %s), %s)",
                 (token_hash(token), user["id"], SESSION_HOURS, user["role"] not in MFA_ROLES))
    db.audit(conn, user_name, "auth.signed_in", {"method": method, "mfa_needed": user["role"] in MFA_ROLES})
    return token


def totp(secret, step):
    digest = hmac.new(base64.b32decode(secret), struct.pack(">Q", step), "sha1").digest()
    offset = digest[-1] & 15
    return f"{(struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def mfa_step(me):
    """What the code step shows: {"mfa": "code"}, or a new secret to enrol {"mfa": "enroll", "secret", "uri"}."""
    with db.tenant(me["tenant_id"]) as conn:
        user = conn.execute("SELECT totp_secret, totp_pending FROM users WHERE name = %s FOR UPDATE",
                            (me["user"],)).fetchone()
        if user["totp_secret"]:
            return {"mfa": "code"}
        secret = user["totp_pending"] or base64.b32encode(secrets.token_bytes(20)).decode()
        conn.execute("UPDATE users SET totp_pending = %s WHERE name = %s", (secret, me["user"]))
        tenant = conn.execute("SELECT name FROM tenants").fetchone()["name"]
    label = quote(f"Rezztor Orchestrator:{me['user']}@{tenant}")
    return {"mfa": "enroll", "secret": secret,
            "uri": f"otpauth://totp/{label}?secret={secret}&issuer=Rezztor%20Orchestrator"}


def check_code(token, code):
    """Finish the code step of a session. A wrong code counts like a wrong password and too many end the session."""
    me = session(token)
    if not me or me["mfa_ok"] or not (isinstance(code, str) and code.isdigit() and len(code) == 6):
        return False
    key = (str(me["tenant_id"]), me["user"])
    now = int(time.time()) // 30
    with db.tenant(me["tenant_id"]) as conn:
        user = conn.execute("SELECT totp_secret, totp_pending, totp_last_step FROM users WHERE name = %s FOR UPDATE",
                            (me["user"],)).fetchone()
        secret = user["totp_secret"] or user["totp_pending"]
        step = next((s for s in (now - 1, now, now + 1)
                     if s > user["totp_last_step"] and secret and hmac.compare_digest(totp(secret, s), code)), None)
        if step is None:
            db.audit(conn, me["user"], "auth.mfa_failed")
        else:
            conn.execute("UPDATE users SET totp_secret = %s, totp_pending = NULL, totp_last_step = %s WHERE name = %s",
                         (secret, step, me["user"]))
            conn.execute("UPDATE sessions SET mfa_ok = true WHERE token_hash = %s", (token_hash(token),))
            db.audit(conn, me["user"], "auth.mfa_enrolled" if not user["totp_secret"] else "auth.mfa_ok")
    if step is None:
        _failed(key)
        with db.app() as conn:
            failures = conn.execute("SELECT failures FROM sign_in_failures WHERE tenant_name = %s AND user_name = %s",
                                    key).fetchone()["failures"]
        if failures >= MAX_FAILURES:
            with db.tenant(me["tenant_id"]) as conn:
                conn.execute("DELETE FROM sessions WHERE token_hash = %s", (token_hash(token),))
        return False
    with db.app() as conn:
        conn.execute("DELETE FROM sign_in_failures WHERE tenant_name = %s AND user_name = %s", key)
    return True


def reset_mfa(tenant_id, user_name, actor="cli"):
    """Forget a user's authenticator (lost phone): the next sign-in enrols a new one."""
    with db.tenant(tenant_id) as conn:
        if conn.execute("UPDATE users SET totp_secret = NULL, totp_pending = NULL WHERE name = %s",
                        (user_name,)).rowcount != 1:
            raise LookupError(f"no user {user_name!r}")
        conn.execute("DELETE FROM sessions WHERE user_id = (SELECT id FROM users WHERE name = %s)", (user_name,))
        db.audit(conn, actor, "auth.mfa_reset", {"user": user_name})


def _failed(key):
    """Count a failure; one after the lockout has passed starts the count again."""
    with db.app() as conn:
        conn.execute("DELETE FROM sign_in_failures WHERE last_at < now() - make_interval(secs => %s)",
                     (LOCKOUT_SECONDS,))  # names that were only typed once in a while do not pile up
        conn.execute("INSERT INTO sign_in_failures (tenant_name, user_name, failures) VALUES (%s, %s, 1) "
                     "ON CONFLICT (tenant_name, user_name) DO UPDATE SET failures = sign_in_failures.failures + 1, "
                     "last_at = now()", key)
    return None


def session(token):
    """Return {"tenant_id", "user", "role", "mfa_ok"} for a live session token, or None. A session without mfa_ok
    opens only the code step."""
    if not token:
        return None
    with db.app() as conn:
        tenant_id = conn.execute("SELECT session_tenant(%s) AS id", (token_hash(token),)).fetchone()["id"]
    if tenant_id is None:
        return None
    with db.tenant(tenant_id) as conn:
        row = conn.execute("SELECT u.name, u.role, s.mfa_ok FROM sessions s JOIN users u ON u.id = s.user_id "
                           "WHERE s.token_hash = %s", (token_hash(token),)).fetchone()
    return row and {"tenant_id": tenant_id, "user": row["name"], "role": row["role"], "mfa_ok": row["mfa_ok"]}


def sign_out(token):
    me = session(token)
    if me:
        with db.tenant(me["tenant_id"]) as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = %s", (token_hash(token),))
            db.audit(conn, me["user"], "auth.signed_out")
