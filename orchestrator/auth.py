"""Passwords and sessions. Directory sign-in (OIDC, SAML) replaces passwords for enterprises in a later phase."""
import hashlib
import hmac
import secrets
import threading
import time

from . import db

SESSION_HOURS = 8
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
_failures, _lock = {}, threading.Lock()
# ponytail: failed sign-ins counted in this process's memory; a shared counter when there is more than one web process.
MAX_FAILURES, LOCKOUT_SECONDS = 5, 300


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
    with _lock:
        count, since = _failures.get(key, (0, 0))
        if count >= MAX_FAILURES and time.monotonic() - since < LOCKOUT_SECONDS:
            return None
    with db.app() as conn:
        tenant_id = conn.execute("SELECT tenant_id_by_name(%s) AS id", (tenant_name,)).fetchone()["id"]
    if tenant_id is None:
        check_password(password, None)
        return _failed(key)
    with db.tenant(tenant_id) as conn:
        user = conn.execute("SELECT id, password_hash FROM users WHERE name = %s", (user_name,)).fetchone()
        if not check_password(password, user and user["password_hash"]):
            db.audit(conn, user_name, "auth.sign_in_failed")
            return _failed(key)
        conn.execute("DELETE FROM sessions WHERE expires_at < now()")
        token = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO sessions (token_hash, tenant_id, user_id, expires_at) VALUES "
                     "(%s, current_setting('app.tenant_id')::uuid, %s, now() + make_interval(hours => %s))",
                     (token_hash(token), user["id"], SESSION_HOURS))
        db.audit(conn, user_name, "auth.signed_in")
    with _lock:
        _failures.pop(key, None)
    return tenant_id, token


def _failed(key):
    with _lock:
        count, _ = _failures.get(key, (0, 0))
        _failures[key] = (count + 1, time.monotonic())
    return None


def session(token):
    """Return {"tenant_id", "user", "role"} for a live session token, or None."""
    if not token:
        return None
    with db.app() as conn:
        tenant_id = conn.execute("SELECT session_tenant(%s) AS id", (token_hash(token),)).fetchone()["id"]
    if tenant_id is None:
        return None
    with db.tenant(tenant_id) as conn:
        row = conn.execute("SELECT u.name, u.role FROM sessions s JOIN users u ON u.id = s.user_id "
                           "WHERE s.token_hash = %s", (token_hash(token),)).fetchone()
    return row and {"tenant_id": tenant_id, "user": row["name"], "role": row["role"]}


def sign_out(token):
    me = session(token)
    if me:
        with db.tenant(me["tenant_id"]) as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = %s", (token_hash(token),))
            db.audit(conn, me["user"], "auth.signed_out")
