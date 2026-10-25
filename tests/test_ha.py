"""More than one copy of each process: web, scheduler and SIEM forwarder share their state through the database."""
import socket

import psycopg

from orchestrator import auth, changes, db, siem
from tests.test_changes import acme, change_cap, propose  # noqa: F401  (acme is a fixture)
from tests.test_core import audit_kinds
from tests.test_web import call, site  # noqa: F401  (site is a fixture)


def test_the_lockout_is_shared_and_ends(database):
    t = db.create_tenant("acme", "alice")
    auth.set_password(t, "alice", "correct horse battery")
    for _ in range(auth.MAX_FAILURES):
        assert auth.sign_in("acme", "alice", "wrong password!") is None
    with db.app() as conn:  # what every web process reads
        assert conn.execute("SELECT failures FROM sign_in_failures").fetchone()["failures"] == auth.MAX_FAILURES
    assert auth.sign_in("acme", "alice", "correct horse battery") is None
    with psycopg.connect(database, autocommit=True) as conn:
        conn.execute("UPDATE sign_in_failures SET last_at = now() - interval '10 minutes'")
    assert auth.sign_in("acme", "alice", "correct horse battery") is not None
    with db.app() as conn:
        assert conn.execute("SELECT count(*) AS n FROM sign_in_failures").fetchone()["n"] == 0


def test_a_second_siem_forwarder_waits_its_turn(acme, database):
    with db.tenant(acme) as conn:
        db.audit(conn, "alice", "test")
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        target = f"tcp://127.0.0.1:{server.getsockname()[1]}"
        with psycopg.connect(database, autocommit=True) as other:
            other.execute("SELECT pg_advisory_lock(%s)", (siem.LOCK,))
            assert siem.forward_once(database, target) == 0
        assert siem.forward_once(database, target) == 1


def test_a_run_whose_process_died_is_marked_failed(acme, database):
    caps = change_cap([])
    change = propose(acme, "oscar", caps)["untrusted_tool_output"]["result"]["change"]
    changes.decide(acme, "ava", change, "d" * 64, True)
    changes.start(acme, "oscar", change)  # and then the process dies
    assert changes.sweep_lost() == 0
    with psycopg.connect(database, autocommit=True) as conn:
        conn.execute("UPDATE changes SET updated_at = now() - interval '4 hours'")
    assert changes.sweep_lost() == 1 and changes.sweep_lost() == 0
    with db.tenant(acme) as conn:
        row = conn.execute("SELECT state, result FROM changes").fetchone()
    assert row["state"] == "FAILED" and row["result"]["outcome"] == "lost" and "change.lost" in audit_kinds(acme)


def test_the_health_check_needs_no_sign_in(site):
    assert call(site, "/healthz")[:2] == (200, {"ok": True})
