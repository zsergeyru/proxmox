"""Minimal allowlisted machine plan callback for physical Ansible.
Never print task results, stderr, SSH details or arbitrary YAML messages.
Only the one named plan publication may emit validated JSON.
"""
from __future__ import annotations

import json
import re
from ansible.plugins.callback import CallbackBase


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "stdout"
    CALLBACK_NAME = "infra_storage_plan"

    def v2_runner_on_ok(self, result):
        if not str(result._task.get_name()).endswith("Опубликовать план только через allowlisted stdout callback"):
            return
        raw = result._result.get("msg")
        prefix = "PHYSICAL_STORAGE_PLAN_JSON="
        if not isinstance(raw, str) or not raw.startswith(prefix) or len(raw) > 5000:
            return
        try:
            payload = json.loads(raw[len(prefix):])
        except (ValueError, TypeError):
            return
        if not isinstance(payload, dict) or set(payload) != {
            "host", "fingerprint", "requires_creation",
            "recovery_fingerprint", "items",
        }:
            return
        if (
            not isinstance(payload["host"], str)
            or not re.fullmatch(r"[a-z][a-z0-9-]*", payload["host"])
            or not isinstance(payload["fingerprint"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", payload["fingerprint"])
            or not isinstance(payload["items"], list)
            or not 1 <= len(payload["items"]) <= 25
            or not isinstance(payload["requires_creation"], bool)
            or payload["recovery_fingerprint"] is not None
        ):
            return
        self._display.display(prefix + json.dumps(payload, ensure_ascii=True, separators=(",", ":")))

    def v2_runner_on_failed(self, result, ignore_errors=False):
        del result, ignore_errors
        self._display.display("ОШИБКА: Ansible не подтвердил физический план дисков")

    def v2_runner_on_unreachable(self, result):
        del result
        self._display.display("ОШИБКА: физический сервер недоступен")
