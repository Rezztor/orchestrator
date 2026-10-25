"""python -m orchestrator COMMAND

  migrate
  tenant-add NAME ADMIN                          prints the new tenant's id
  user-add TENANT_ID NAME ROLE                   tenant-admin, approver, operator or viewer
  set-password TENANT_ID USER                    asks for the password
  device-add TENANT_ID NAME ADDRESS PLATFORM [SITE]
  device-critical TENANT_ID NAME yes|no          a change to a critical device is one risk class higher
  ask TENANT_ID USER PROMPT
  audit-verify TENANT_ID                         the hash chain, and the signed anchors when ORCH_ANCHOR_PUB is set
  oidc-set TENANT_ID ISSUER CLIENT_ID SECRET_ENV [CLAIM]
                                                 directory sign-in; SECRET_ENV names the client secret's variable
  mfa-reset TENANT_ID USER                       forget a lost authenticator; the next sign-in enrols a new one
  policy-set TENANT_ID FILE                      the tenant's policy (YAML); it can only narrow the shipped one
  policy-show TENANT_ID                          the effective policy
  stop TENANT_ID REASON | resume TENANT_ID       the tenant's kill switch
  stop-all REASON | resume-all                   the installation's kill switch (owner role)
  skill-add TENANT_ID FILE                       a SKILL.md, approved for the tenant
  recipe-add TENANT_ID NAME FILE                 YAML: description, prompt with {placeholders}
  recipe-run TENANT_ID USER NAME [KEY=VALUE ...]
  schedule-add TENANT_ID RECIPE USER MINUTES [KEY=VALUE ...]   run the recipe as USER every MINUTES (5 or more)
  scheduler                                      run due schedules and fail lost runs, forever
  webhook-add TENANT_ID teams|slack|json URL     post change and kill-switch events there
  siem-forward                                   send the audit ledger to ORCH_SIEM_TARGET, forever (owner role)
  eval FILE                                      run eval cases against the model, each in a new eval-* tenant
"""
import getpass
import json
import os
import sys

import psycopg

from . import anchors, auth, db, evals, loop, oidc, notify, policy, recipes, siem, skills


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
        case ["device-critical", tenant_id, name, ("yes" | "no") as critical]:
            with db.tenant(tenant_id) as conn:
                if not conn.execute("UPDATE devices SET critical = %s WHERE name = %s",
                                    (critical == "yes", name)).rowcount:
                    print(f"no device {name!r}", file=sys.stderr)
                    return 1
                db.audit(conn, "cli", "device.critical", {"device": name, "critical": critical == "yes"})
        case ["ask", tenant_id, user, prompt]:
            request_id, state, answer = loop.run(tenant_id, user, prompt)
            print(f"{state} {request_id}\n{answer}")
            return 0 if state == "DONE" else 1
        case ["audit-verify", tenant_id]:
            with db.tenant(tenant_id) as conn:
                broken = db.verify_audit(conn)
                checked, bad = (anchors.verify(conn, tenant_id, os.environ["ORCH_ANCHOR_PUB"])
                                if os.environ.get("ORCH_ANCHOR_PUB") else (0, None))
            print("audit chain holds" if broken is None else f"audit chain broken at seq {broken}")
            if os.environ.get("ORCH_ANCHOR_PUB"):
                print(f"{checked} signed anchors hold" if bad is None else f"signed anchor at seq {bad} does not hold")
            return 0 if broken is None and bad is None else 1
        case ["oidc-set", tenant_id, issuer, client_id, secret_env, *claim]:
            oidc.set_config(tenant_id, issuer, client_id, secret_env, *claim[:1])
        case ["mfa-reset", tenant_id, user]:
            auth.reset_mfa(tenant_id, user)
        case ["policy-set", tenant_id, path]:
            text = open(path).read()
            policy.parse(text)
            with db.tenant(tenant_id) as conn:
                conn.execute("INSERT INTO policies (tenant_id, document, set_by) "
                             "VALUES (current_setting('app.tenant_id')::uuid, %s, 'cli') ON CONFLICT (tenant_id) "
                             "DO UPDATE SET document = EXCLUDED.document, set_by = 'cli', set_at = now()", (text,))
                db.audit(conn, "cli", "policy.set", {"digest": db.digest(text)})
        case ["policy-show", tenant_id]:
            with db.tenant(tenant_id) as conn:
                print(json.dumps(policy.for_tenant(conn), indent=2))
        case ["stop", tenant_id, reason]:
            loop.set_kill_switch(tenant_id, "cli", reason)
        case ["resume", tenant_id]:
            loop.set_kill_switch(tenant_id, "cli", None)
        case ["stop-all", reason]:
            with psycopg.connect(os.environ["ORCH_DB_URL"]) as conn:
                conn.execute("INSERT INTO installation_kill (reason, set_by) VALUES (%s, 'cli') ON CONFLICT (only_row) "
                             "DO UPDATE SET reason = EXCLUDED.reason, set_at = now()", (reason,))
        case ["resume-all"]:
            with psycopg.connect(os.environ["ORCH_DB_URL"]) as conn:
                conn.execute("DELETE FROM installation_kill")
        case ["skill-add", tenant_id, path]:
            with db.tenant(tenant_id) as conn:
                skills.add(conn, "cli", skills.parse(open(path).read()), "tenant", "approved")
        case ["recipe-add", tenant_id, name, path]:
            recipes.add(tenant_id, "cli", name, open(path).read())
        case ["recipe-run", tenant_id, user, name, *pairs]:
            request_id, state, answer = recipes.run(tenant_id, user, name, dict(p.split("=", 1) for p in pairs))
            print(f"{state} {request_id}\n{answer}")
            return 0 if state == "DONE" else 1
        case ["schedule-add", tenant_id, recipe, user, minutes, *pairs]:
            print(recipes.schedule(tenant_id, "cli", recipe, user, int(minutes), dict(p.split("=", 1) for p in pairs)))
        case ["scheduler"]:
            recipes.scheduler()
        case ["webhook-add", tenant_id, kind, url]:
            notify.add(tenant_id, "cli", kind, url)
        case ["siem-forward"]:
            siem.forward()
        case ["eval", path]:
            return 0 if evals.run(path) else 1
        case _:
            print(__doc__, file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
