"""python -m orchestrator COMMAND

  migrate
  tenant-add NAME ADMIN                          prints the new tenant's id
  user-add TENANT_ID NAME ROLE                   tenant-admin, approver, operator or viewer
  set-password TENANT_ID USER                    asks for the password
  device-add TENANT_ID NAME ADDRESS PLATFORM [SITE]
  ask TENANT_ID USER PROMPT
  audit-verify TENANT_ID
  policy-set TENANT_ID FILE                      the tenant's policy (YAML); it can only narrow the shipped one
  policy-show TENANT_ID                          the effective policy
  stop TENANT_ID REASON | resume TENANT_ID       the tenant's kill switch
  stop-all REASON | resume-all                   the installation's kill switch (owner role)
"""
import getpass
import json
import os
import sys

import psycopg

from . import auth, db, loop, policy


def main(argv):
    match argv:
        case ["migrate"]:
            db.migrate()
        case ["tenant-add", name, admin]:
            print(db.create_tenant(name, admin))
        case ["user-add", tenant_id, name, role]:
            with db.tenant(tenant_id) as conn:
                conn.execute("INSERT INTO users (tenant_id, name, role) "
                             "VALUES (current_setting('app.tenant_id')::uuid, %s, %s)", (name, role))
                db.audit(conn, "cli", "user.added", {"user": name, "role": role})
        case ["set-password", tenant_id, user]:
            auth.set_password(tenant_id, user, getpass.getpass(f"New password for {user}: "))
        case ["device-add", tenant_id, name, address, platform, *site]:
            with db.tenant(tenant_id) as conn:
                conn.execute("INSERT INTO devices (tenant_id, name, address, platform, site) "
                             "VALUES (current_setting('app.tenant_id')::uuid, %s, %s, %s, %s)",
                             (name, address, platform, site[0] if site else ""))
                db.audit(conn, "cli", "device.added", {"device": name})
        case ["ask", tenant_id, user, prompt]:
            request_id, state, answer = loop.run(tenant_id, user, prompt)
            print(f"{state} {request_id}\n{answer}")
            return 0 if state == "DONE" else 1
        case ["audit-verify", tenant_id]:
            with db.tenant(tenant_id) as conn:
                broken = db.verify_audit(conn)
            print("audit chain holds" if broken is None else f"audit chain broken at seq {broken}")
            return 0 if broken is None else 1
        case ["policy-set", tenant_id, path]:
            text = open(path).read()
            policy.parse(text)
            with db.tenant(tenant_id) as conn:
                conn.execute("INSERT INTO policies (tenant_id, document, set_by) "
                             "VALUES (current_setting('app.tenant_id')::uuid, %s, 'cli') ON CONFLICT (tenant_id) "
                             "DO UPDATE SET document = EXCLUDED.document, set_by = 'cli', set_at = now()", (text,))
                db.audit(conn, "cli", "policy.set", {"digest": loop.digest(text)})
        case ["policy-show", tenant_id]:
            with db.tenant(tenant_id) as conn:
                print(json.dumps(policy.for_tenant(conn), indent=2))
        case ["stop", tenant_id, reason]:
            with db.tenant(tenant_id) as conn:
                loop.set_kill_switch(conn, "cli", reason)
        case ["resume", tenant_id]:
            with db.tenant(tenant_id) as conn:
                loop.set_kill_switch(conn, "cli", None)
        case ["stop-all", reason]:
            with psycopg.connect(os.environ["ORCH_DB_URL"]) as conn:
                conn.execute("INSERT INTO installation_kill (reason, set_by) VALUES (%s, 'cli') ON CONFLICT (only_row) "
                             "DO UPDATE SET reason = EXCLUDED.reason, set_at = now()", (reason,))
        case ["resume-all"]:
            with psycopg.connect(os.environ["ORCH_DB_URL"]) as conn:
                conn.execute("DELETE FROM installation_kill")
        case _:
            print(__doc__, file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
