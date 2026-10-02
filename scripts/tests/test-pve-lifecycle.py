#!/usr/bin/env python3
"""Проверки приёмочного PVE lifecycle test без изменений инфраструктуры."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = ROOT / "scripts" / "infra-manager"
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager import pve_lifecycle as lifecycle
from infra_manager.common import InfraManagerError


class FakePveClient:
    def __init__(self, *, occupied: bool = False, fail_create_wait: bool = False) -> None:
        self.exists = occupied
        self.status = "stopped"
        self.fail_create_wait = fail_create_wait
        self.create_form: dict[str, str | int] | None = None
        self.actions: list[str] = []
        self.memory = 256

    def data(self, endpoint: str, *, query=None):
        if endpoint == "/nodes":
            return [{"node": "pve"}]
        if endpoint == "/nodes/pve/storage/local/content":
            assert query == {"content": "vztmpl"}
            return [
                {"volid": "local:vztmpl/debian-13-standard_13.9-1_amd64.tar.zst"},
                {"volid": "local:vztmpl/debian-13-standard_13.10-1_amd64.tar.zst"},
            ]
        if endpoint == "/pools/managed":
            return {
                "members": (
                    [{"vmid": lifecycle.TEST_VMID}]
                    if self.exists
                    else []
                )
            }
        if endpoint == f"/nodes/pve/lxc/{lifecycle.TEST_VMID}/config":
            return {"memory": self.memory}
        if endpoint == f"/nodes/pve/lxc/{lifecycle.TEST_VMID}/status/current":
            return {"status": self.status}
        raise AssertionError(f"Неожиданный GET: {endpoint}")

    def find_vm(self, vmid: int):
        assert vmid == lifecycle.TEST_VMID
        if not self.exists:
            return None
        return {
            "vmid": vmid,
            "type": "lxc",
            "name": lifecycle.TEST_HOSTNAME,
        }

    def request_data(self, method: str, endpoint: str, *, form=None, query=None):
        assert method == "POST"
        assert endpoint == "/nodes/pve/lxc"
        assert query is None
        self.create_form = dict(form or {})
        self.exists = True
        self.actions.append("create")
        return "UPID:create"

    def wait_task(self, *, node: str, upid: str, timeout: int = 180) -> None:
        assert node == "pve"
        del timeout
        if upid == "UPID:create" and self.fail_create_wait:
            raise InfraManagerError("ожидаемый сбой создания")

    def put(self, endpoint: str, *, form=None):
        assert endpoint == f"/nodes/pve/lxc/{lifecycle.TEST_VMID}/config"
        self.memory = int((form or {})["memory"])
        self.actions.append("update")
        return {"data": None}

    def run_task(
        self,
        method: str,
        endpoint: str,
        *,
        node: str,
        query=None,
        form=None,
        timeout: int = 180,
    ) -> None:
        assert node == "pve"
        del query, form, timeout
        if endpoint.endswith("/status/start"):
            assert method == "POST"
            self.status = "running"
            self.actions.append("start")
        elif endpoint.endswith("/status/stop"):
            assert method == "POST"
            self.status = "stopped"
            self.actions.append("stop")
        elif endpoint == f"/nodes/pve/lxc/{lifecycle.TEST_VMID}":
            assert method == "DELETE"
            self.exists = False
            self.actions.append("delete")
        else:
            raise AssertionError(f"Неожиданная задача: {method} {endpoint}")


def expect_error(callable_, message: str) -> None:
    try:
        callable_()
    except InfraManagerError:
        return
    raise SystemExit(message)


def test_success() -> None:
    client = FakePveClient()
    with (
        patch.object(lifecycle.os, "geteuid", return_value=0),
        patch.object(lifecycle, "PveClient", return_value=client),
    ):
        result = lifecycle.run_lifecycle_test(apply=True)

    if result != 0:
        raise SystemExit("Успешный lifecycle test вернул ненулевой код")
    if client.exists:
        raise SystemExit("Временный LXC остался после успешного теста")
    if client.actions != ["create", "update", "start", "stop", "delete"]:
        raise SystemExit(f"Неверный жизненный цикл: {client.actions!r}")
    if client.create_form is None:
        raise SystemExit("Не зафиксированы параметры создания LXC")
    expected_template = "local:vztmpl/debian-13-standard_13.10-1_amd64.tar.zst"
    if client.create_form.get("ostemplate") != expected_template:
        raise SystemExit("Lifecycle test выбрал не самый новый Debian 13 template")
    if client.create_form.get("pool") != "managed":
        raise SystemExit("Тестовый LXC должен сразу создаваться в pool managed")


def test_requires_apply() -> None:
    expect_error(
        lambda: lifecycle.run_lifecycle_test(apply=False),
        "Lifecycle test разрешил изменения без --apply",
    )


def test_rejects_occupied_vmid() -> None:
    client = FakePveClient(occupied=True)
    with (
        patch.object(lifecycle.os, "geteuid", return_value=0),
        patch.object(lifecycle, "PveClient", return_value=client),
    ):
        expect_error(
            lambda: lifecycle.run_lifecycle_test(apply=True),
            "Lifecycle test принял уже занятый VMID",
        )
    if client.actions:
        raise SystemExit("При занятом VMID тест не должен ничего изменять")


def test_cleanup_after_create_failure() -> None:
    client = FakePveClient(fail_create_wait=True)
    with (
        patch.object(lifecycle.os, "geteuid", return_value=0),
        patch.object(lifecycle, "PveClient", return_value=client),
    ):
        expect_error(
            lambda: lifecycle.run_lifecycle_test(apply=True),
            "Ожидаемый сбой создания не был передан вызывающему коду",
        )
    if client.exists:
        raise SystemExit("После сбоя не удалён принадлежащий тесту LXC")
    if client.actions != ["create", "delete"]:
        raise SystemExit(
            f"Аварийная очистка выполнила неожиданные действия: {client.actions!r}"
        )


def test_cleanup_refuses_foreign_object() -> None:
    client = FakePveClient(occupied=True)

    def foreign_find_vm(vmid: int):
        assert vmid == lifecycle.TEST_VMID
        return {
            "vmid": vmid,
            "type": "lxc",
            "name": "foreign-object",
        }

    client.find_vm = foreign_find_vm  # type: ignore[method-assign]
    expect_error(
        lambda: lifecycle._cleanup_created_lxc(client, "pve"),
        "Аварийная очистка разрешила удалить чужой объект",
    )
    if client.actions:
        raise SystemExit("Чужой объект не должен изменяться при аварийной очистке")


def main() -> None:
    test_success()
    test_requires_apply()
    test_rejects_occupied_vmid()
    test_cleanup_after_create_failure()
    test_cleanup_refuses_foreign_object()
    print("[ОК] Проверки PVE lifecycle test пройдены")


if __name__ == "__main__":
    main()
