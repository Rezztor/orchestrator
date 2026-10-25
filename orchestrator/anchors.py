"""Signed anchors on the audit ledger.

The hash chain shows that rows fit together, not that nobody with the database owner's password rewrote them all.
Once an hour the scheduler signs each tenant's newest row (its seq and hash) with the installation's Ed25519 key and
appends the signature to the ledger as an audit.anchor row, which the SIEM forwarder also sends out. A rewrite then
breaks an anchor signed before it, or an anchor copy the SIEM keeps.

    ORCH_ANCHOR_KEY   Ed25519 private key (PEM) for the scheduler: openssl genpkey -algorithm ed25519
    ORCH_ANCHOR_PUB   its public key (PEM) for audit-verify
"""
import os
import uuid
from datetime import timedelta

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.serialization import load_pem_private_key, load_pem_public_key

from . import db

EVERY = timedelta(hours=1)


def message(tenant_id, seq, hash_):
    return f"rezztor-orchestrator-anchor:{uuid.UUID(str(tenant_id))}:{seq}:{hash_}".encode()


def anchor_all(key_path=None, every=EVERY):
    """Anchor every tenant whose ledger grew and whose last anchor is older than `every`. Returns how many."""
    key_path = key_path or os.environ.get("ORCH_ANCHOR_KEY")
    if not key_path:
        return 0
    key = load_pem_private_key(open(key_path, "rb").read(), None)
    with db.app() as conn:
        tenants = [r["id"] for r in conn.execute("SELECT id FROM tenant_ids()")]
    made = 0
    for tenant_id in tenants:
        with db.tenant(tenant_id) as conn:
            last = conn.execute("SELECT seq, hash, kind FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
            if last is None or last["kind"] == "audit.anchor":
                continue
            if conn.execute("SELECT 1 FROM audit WHERE kind = 'audit.anchor' AND at > now() - %s",
                            (every,)).fetchone():
                continue
            signature = key.sign(message(tenant_id, last["seq"], last["hash"])).hex()
            db.audit(conn, "anchor", "audit.anchor", {"seq": last["seq"], "hash": last["hash"], "signature": signature})
            made += 1
    return made


def verify(conn, tenant_id, pub_path):
    """Check every anchor in this tenant's ledger. Returns (anchors checked, the seq of the first bad one or None)."""
    pub = load_pem_public_key(open(pub_path, "rb").read())
    hashes = {r["seq"]: r["hash"] for r in conn.execute("SELECT seq, hash FROM audit")}
    anchors = conn.execute("SELECT seq, data FROM audit WHERE kind = 'audit.anchor' ORDER BY seq").fetchall()
    for row in anchors:
        a = row["data"]
        try:
            pub.verify(bytes.fromhex(a["signature"]), message(tenant_id, a["seq"], a["hash"]))
        except (InvalidSignature, ValueError, KeyError):
            return len(anchors), row["seq"]
        if hashes.get(a["seq"]) != a["hash"]:
            return len(anchors), row["seq"]
    return len(anchors), None
