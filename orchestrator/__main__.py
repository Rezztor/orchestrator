"""python -m orchestrator migrate | tenant-add NAME ADMIN | ask TENANT_ID USER PROMPT | audit-verify TENANT_ID"""
import sys

from . import db, loop


def main(argv):
    match argv:
        case ["migrate"]:
            db.migrate()
        case ["tenant-add", name, admin]:
            print(db.create_tenant(name, admin))
        case ["ask", tenant_id, user, prompt]:
            request_id, state, answer = loop.run(tenant_id, user, prompt)
            print(f"{state} {request_id}\n{answer}")
            return 0 if state == "DONE" else 1
        case ["audit-verify", tenant_id]:
            with db.tenant(tenant_id) as conn:
                broken = db.verify_audit(conn)
            print("audit chain holds" if broken is None else f"audit chain broken at seq {broken}")
            return 0 if broken is None else 1
        case _:
            print(__doc__, file=sys.stderr)
            return 2
    return 0


sys.exit(main(sys.argv[1:]))
