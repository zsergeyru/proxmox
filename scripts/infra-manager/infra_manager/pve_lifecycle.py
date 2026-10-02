"""Приёмочная проверка полного жизненного цикла LXC через PVE API."""

from __future__ import annotations

import os
import re
import sys
from typing import Any

from .common import InfraManagerError, console
from .pve import PveClient

TEST_VMID = 9098
TEST_HOSTNAME = "infra-access-test"
MANAGED_POOL = "managed"
ROOT_STORAGE = "local-lvm"
TEMPLATE_STORAGE = "local"
BRIDGE = "vmbr0"

_TEMPLATE_PATTERN = re.compile(
    r"(?:^|:)vztmpl/debian-13-standard_.*_amd64[.]tar[.](?:zst|gz)$"
)


def _version_key(value: str) -> tuple[int, ...]:
    """Получить устойчивый числовой ключ версии имени шаблона."""
    return tuple(int(item) for item in re.findall(r"[0-9]+", value))


def _pve_node(client: PveClient) -> str:
    nodes = client.data("/nodes")
    if not isinstance(nodes, list):
        raise InfraManagerError("PVE API /nodes вернул неожиданный формат")
    for item in nodes:
        if isinstance(item, dict):
            node = item.get("node")
            if isinstance(node, str) and node:
                return node
    raise InfraManagerError("Не удалось определить PVE node")


def _debian_template(client: PveClient, node: str) -> str:
    content = client.data(
        f"/nodes/{node}/storage/{TEMPLATE_STORAGE}/content",
        query={"content": "vztmpl"},
    )
    if not isinstance(content, list):
        raise InfraManagerError(
            f"PVE storage {TEMPLATE_STORAGE} вернул неожиданный формат"
        )

    candidates = [
        volid
        for item in content
        if isinstance(item, dict)
        and isinstance((volid := item.get("volid")), str)
        and _TEMPLATE_PATTERN.search(volid)
    ]
    if not candidates:
        raise InfraManagerError(
            f"Не найден Debian 13 LXC template на {TEMPLATE_STORAGE}"
        )
    return max(candidates, key=_version_key)


def _pool_has_vmid(client: PveClient, vmid: int) -> bool:
    payload = client.data(f"/pools/{MANAGED_POOL}")
    if not isinstance(payload, dict):
        raise InfraManagerError(
            f"PVE pool {MANAGED_POOL} вернул неожиданный формат"
        )
    members = payload.get("members")
    if not isinstance(members, list):
        return False
    return any(
        isinstance(item, dict) and str(item.get("vmid", "")) == str(vmid)
        for item in members
    )


def _lxc_status(client: PveClient, node: str) -> str:
    payload = client.data(
        f"/nodes/{node}/lxc/{TEST_VMID}/status/current"
    )
    if not isinstance(payload, dict):
        raise InfraManagerError(
            f"PVE status LXC {TEST_VMID} имеет неожиданный формат"
        )
    return str(payload.get("status") or "")


def _cleanup_created_lxc(client: PveClient, node: str) -> None:
    """Удалить только принадлежащий этому тесту временный LXC."""
    resource = client.find_vm(TEST_VMID)
    if resource is None:
        return

    actual_type = str(resource.get("type") or "")
    actual_name = str(resource.get("name") or "")
    if actual_type != "lxc" or actual_name != TEST_HOSTNAME:
        raise InfraManagerError(
            f"VMID {TEST_VMID} после сбоя занят объектом "
            f"{actual_name!r} типа {actual_type!r}; автоматическое удаление запрещено"
        )

    if _lxc_status(client, node) == "running":
        client.run_task(
            "POST",
            f"/nodes/{node}/lxc/{TEST_VMID}/status/stop",
            node=node,
        )
    client.run_task(
        "DELETE",
        f"/nodes/{node}/lxc/{TEST_VMID}",
        node=node,
    )


def run_lifecycle_test(*, apply: bool) -> int:
    """Создать, изменить, запустить, остановить и удалить тестовый LXC."""
    if not apply:
        raise InfraManagerError(
            "Для запуска теста требуется параметр --apply"
        )
    if os.geteuid() != 0:
        raise InfraManagerError(
            "Запустите тест от root внутри infra-manager"
        )

    client = PveClient()
    node = _pve_node(client)

    if client.find_vm(TEST_VMID) is not None:
        raise InfraManagerError(
            f"VMID {TEST_VMID} уже занят; тест ничего не удаляет"
        )

    template = _debian_template(client, node)
    console.info(
        f"Будет создан временный LXC {TEST_VMID} на node {node} из {template}"
    )

    owns_test_vmid = False
    try:
        upid: Any = client.request_data(
            "POST",
            f"/nodes/{node}/lxc",
            form={
                "vmid": TEST_VMID,
                "hostname": TEST_HOSTNAME,
                "pool": MANAGED_POOL,
                "ostemplate": template,
                "ostype": "debian",
                "unprivileged": 1,
                "cores": 1,
                "memory": 256,
                "swap": 0,
                "rootfs": f"{ROOT_STORAGE}:2",
                "features": "nesting=1",
                "net0": f"name=eth0,bridge={BRIDGE},ip=dhcp,type=veth",
                "onboot": 0,
            },
        )
        if not isinstance(upid, str) or not upid:
            raise InfraManagerError(
                "PVE не вернул task id создания LXC"
            )
        owns_test_vmid = True
        client.wait_task(node=node, upid=upid)
        console.ok(f"LXC {TEST_VMID} создан сразу в {MANAGED_POOL}")

        if not _pool_has_vmid(client, TEST_VMID):
            raise InfraManagerError(
                f"Созданный LXC не найден в pool {MANAGED_POOL}"
            )

        client.put(
            f"/nodes/{node}/lxc/{TEST_VMID}/config",
            form={"memory": 384},
        )
        config = client.data(
            f"/nodes/{node}/lxc/{TEST_VMID}/config"
        )
        if not isinstance(config, dict) or str(config.get("memory")) != "384":
            raise InfraManagerError("Изменение RAM не применилось")
        console.ok("Конфигурация LXC изменена")

        client.run_task(
            "POST",
            f"/nodes/{node}/lxc/{TEST_VMID}/status/start",
            node=node,
        )
        if _lxc_status(client, node) != "running":
            raise InfraManagerError("LXC не запустился")
        console.ok("LXC запущен")

        client.run_task(
            "POST",
            f"/nodes/{node}/lxc/{TEST_VMID}/status/stop",
            node=node,
        )
        if _lxc_status(client, node) != "stopped":
            raise InfraManagerError("LXC не остановился")
        console.ok("LXC остановлен")

        client.run_task(
            "DELETE",
            f"/nodes/{node}/lxc/{TEST_VMID}",
            node=node,
        )
        owns_test_vmid = False

        if client.find_vm(TEST_VMID) is not None:
            raise InfraManagerError(
                f"После удаления VMID {TEST_VMID} всё ещё существует"
            )
        console.ok("Временный LXC удалён")
    finally:
        if owns_test_vmid:
            print(
                f"[ПРЕДУПРЕЖДЕНИЕ] Тест прерван; выполняется попытка "
                f"удалить временный LXC {TEST_VMID}",
                file=sys.stderr,
            )
            try:
                _cleanup_created_lxc(client, node)
            except (InfraManagerError, OSError) as exc:
                print(
                    f"[ПРЕДУПРЕЖДЕНИЕ] Не удалось автоматически удалить "
                    f"LXC {TEST_VMID}: {exc}",
                    file=sys.stderr,
                )

    console.result("Интеграционный тест PVE API успешно завершён")
    return 0
