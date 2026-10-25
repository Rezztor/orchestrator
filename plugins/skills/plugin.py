from orchestrator import skills
from orchestrator.plugins import Refused


def read(conn, name):
    skill = skills.usable(conn).get(name)
    if skill is None:
        raise Refused(f"no skill {name!r}")
    return {"name": name, "procedure": skill["body"]}


def propose(conn, name, description, body, capabilities=()):
    try:
        skills.add(conn, "agent", skills.check(name, description, body, list(capabilities)), "induced", "proposed")
    except ValueError as exc:
        raise Refused(str(exc))
    return {"skill": name, "state": "proposed", "note": "A tenant administrator must approve it before it is used."}
