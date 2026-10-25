"""Ansible stdout callback: one JSON object, {host: {"status": ..., "result": ...}}, printed when the run ends."""
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
        self.hosts[result._host.get_name()] = {"status": status, "result": result._result}

    def v2_runner_on_ok(self, result, **kwargs):
        self._keep("ok", result)

    def v2_runner_on_failed(self, result, ignore_errors=False):
        self._keep("failed", result)

    def v2_runner_on_unreachable(self, result):
        self._keep("unreachable", result)

    def v2_playbook_on_stats(self, stats):
        self._display.display(json.dumps(self.hosts, default=str))
