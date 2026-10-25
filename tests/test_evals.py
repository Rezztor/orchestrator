import yaml

from orchestrator import evals, plugins
from tests.test_core import scripted, tool_call

CASES = [c for p in evals.files() for c in yaml.safe_load(p.read_text())]


def test_every_case_names_real_capabilities():
    known = set(plugins.load()) | {"agents.delegate"}
    assert len({c["name"] for c in CASES}) == len(CASES)
    for case in CASES:
        named = set(case.get("expect", [])) | set(case.get("never", [])) | set(case.get("results", {}))
        assert named <= known, (case["name"], named - known)


def test_a_case_passes_on_the_right_calls_and_fails_on_the_wrong_ones(database):
    case = next(c for c in CASES if c["name"] == "os-version")
    good = scripted(tool_call("ansible__facts__read", {"devices": ["core-sw1"]}), {"content": "It runs 17.9.4a."})
    problems, calls = evals.run_case(case, good)
    assert (problems, calls) == ([], ["ansible.facts.read"])
    assert "17.9.4a" in good.seen[1][-1]["content"]  # the canned result, not an executor

    bad = scripted(tool_call("ansible__playbook__run", {"playbook": "upgrade", "devices": ["core-sw1"]}),
                   {"content": "Upgraded."})
    problems, calls = evals.run_case(case, bad)
    assert calls == ["ansible.playbook.run"]
    assert problems == ["did not call ansible.facts.read", "called ansible.playbook.run", "answer lacks '17.9.4a'"]
