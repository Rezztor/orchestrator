from orchestrator.plugins import known_devices as known, post_job


def job(body, timeout):
    return post_job("ORCH_ANSIBLE", body, timeout)


def read_facts(conn, devices):
    known(conn, devices)
    return job({"capability": "ansible.facts.read", "devices": devices}, 330)["hosts"]


def list_playbooks(conn):
    return job({"capability": "ansible.playbooks.list"}, 30)["playbooks"]


def plan_playbook(conn, playbook, devices, vars=None):
    known(conn, devices)
    plan = job({"capability": "ansible.playbook.run", "phase": "plan", "playbook": playbook, "devices": devices,
                "vars": vars or {}}, 660)
    return {**plan, "targets": devices}


def apply_playbook(digest, playbook, devices, vars=None):
    # plan, apply, verify and perhaps rollback, each up to 10 minutes on the executor
    return job({"capability": "ansible.playbook.run", "phase": "apply", "digest": digest, "playbook": playbook,
                "devices": devices, "vars": vars or {}}, 2500)
