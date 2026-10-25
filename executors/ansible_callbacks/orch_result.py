"""Ansible stdout callback: one JSON object, {host: {"status", "result", "tasks"}}, printed when the run ends.

"status" is the host's first failure (failed, unreachable) or ok; "result" is its last task's raw result; "tasks" has
each task's name, status, changed flag, diff and message.
"""
import json

from ansible.plugins.callback import CallbackBase


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "stdout"
    CALLBACK_NAME = "orch_result"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.hosts = {}

    def _keep(self, status, result):
        host = self.hosts.setdefault(result._host.get_name(), {"status": "ok", "result": {}, "tasks": []})
        host["result"] = result._result
        if host["status"] == "ok" and status in ("failed", "unreachable"):
            host["status"] = status
        host["tasks"].append({"task": result._task.get_name(), "status": status,
                              "changed": bool(result._result.get("changed")), "diff": result._result.get("diff"),
                              "msg": result._result.get("msg")})

    def v2_runner_on_ok(self, result, **kwargs):
        self._keep("ok", result)

    def v2_runner_on_skipped(self, result, **kwargs):
        self._keep("skipped", result)

    def v2_runner_on_failed(self, result, ignore_errors=False):
        self._keep("ignored" if ignore_errors else "failed", result)

    def v2_runner_on_unreachable(self, result):
        self._keep("unreachable", result)

    def v2_playbook_on_stats(self, stats):
        self._display.display(json.dumps(self.hosts, default=str))
