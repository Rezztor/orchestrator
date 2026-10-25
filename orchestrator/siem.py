"""Forward the audit ledger to the customer's SIEM as RFC 5424 syslog, one event per ledger row, JSON in the message.

Runs with the owner role (ORCH_DB_URL): it reads every tenant's ledger and keeps its place per tenant in siem_cursor,
so a restart sends from where it stopped (a row may be sent twice, never skipped). Each tenant's batch commits its
cursor on its own. Run a second forwarder as a standby: an advisory lock lets one send at a time.

    ORCH_SIEM_TARGET   udp://host:514, tcp://host:601 or tls://host:6514
    ORCH_SIEM_CA       CA bundle for tls://

python -m orchestrator siem-forward
"""
import json
import logging
import os
import socket
import ssl
import time
from datetime import timezone
from urllib.parse import urlsplit

import psycopg
from psycopg.rows import dict_row

HOST = socket.gethostname()
LOCK = 0x5ED_1E06  # advisory lock key: one forwarder sends at a time, so a second one is a standby


def line(row):
    at = row["at"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    body = json.dumps({"tenant": str(row["tenant_id"]), "seq": row["seq"], "actor": row["actor"], "kind": row["kind"],
                       "data": row["data"], "hash": row["hash"]}, default=str, separators=(",", ":"))
    # facility 13 (log audit), severity 6 (informational); MSGID is the event kind
    return f"<110>1 {at} {HOST} rezztor-orchestrator - {row['kind'][:32]} - {body}".encode()


def connect(target):
    url = urlsplit(target)
    if url.scheme == "udp":
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect((url.hostname, url.port))
        return sock, False
    if url.scheme not in ("tcp", "tls"):
        raise ValueError("ORCH_SIEM_TARGET is udp://, tcp:// or tls://host:port")
    sock = socket.create_connection((url.hostname, url.port), timeout=30)
    if url.scheme == "tls":
        sock = ssl.create_default_context(cafile=os.environ.get("ORCH_SIEM_CA")).wrap_socket(
            sock, server_hostname=url.hostname)
    return sock, True


def forward_once(owner_url, target, batch=500):
    """Send every tenant's ledger rows past its cursor. Returns how many rows were sent."""
    sent = 0
    with psycopg.connect(owner_url, row_factory=dict_row, autocommit=True) as conn:
        if not conn.execute("SELECT pg_try_advisory_lock(%s) AS got", (LOCK,)).fetchone()["got"]:
            return 0  # another forwarder is sending this round; the lock goes with its connection
        sock, stream = connect(target)
        with sock:
            for tenant in conn.execute("SELECT id FROM tenants ORDER BY created_at").fetchall():
                with conn.transaction():
                    conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant["id"]),))
                    cursor = conn.execute(
                        "INSERT INTO siem_cursor (tenant_id) VALUES (%s) ON CONFLICT (tenant_id) DO UPDATE "
                        "SET last_seq = siem_cursor.last_seq RETURNING last_seq",
                        (tenant["id"],)).fetchone()["last_seq"]
                    rows = conn.execute("SELECT * FROM audit WHERE seq > %s ORDER BY seq LIMIT %s",
                                        (cursor, batch)).fetchall()
                    for row in rows:
                        msg = line(row)
                        sock.sendall(f"{len(msg)} ".encode() + msg if stream else msg)  # RFC 6587 octet counting
                    if rows:
                        conn.execute("UPDATE siem_cursor SET last_seq = %s WHERE tenant_id = %s",
                                     (rows[-1]["seq"], tenant["id"]))
                    sent += len(rows)
    return sent


def forward(every_seconds=10):
    while True:
        try:
            forward_once(os.environ["ORCH_DB_URL"], os.environ["ORCH_SIEM_TARGET"])
        except Exception:  # the SIEM or the database may be away: the cursor makes the next round catch up
            logging.exception("SIEM forwarding round failed")
        time.sleep(every_seconds)
