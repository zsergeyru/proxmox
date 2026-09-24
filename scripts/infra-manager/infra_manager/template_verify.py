"""Smoke-проверка Packer-шаблона через временный Full Clone."""

from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any

from .common import InfraManagerError, console, require_command, run
from .pve import PveClient
from .template import is_truthy

TEST_VMID = 9099
TEST_NAME = "smoke-template-9000"


def _first_ipv4(interfaces: Any) -> str | None:
    records: list[Any]
    if isinstance(interfaces, dict):
        value = interfaces.get("result", interfaces)
    else:
        value = interfaces

    if isinstance(value, list):
        records = value
    elif isinstance(value, dict):
        records = list(value.values())
    else:
        return None

    for interface in records:
        if not isinstance(interface, dict):
            continue
        addresses = interface.get("ip-addresses")
        if not isinstance(addresses, list):
            continue
        for item in addresses:
            if not isinstance(item, dict):
                continue
            if item.get("ip-address-type") != "ipv4":
                continue
            address = str(item.get("ip-address") or "")
            if address and address != "127.0.0.1":
                return address
    return None


def _ssh_base(key: Path, known_hosts: Path, address: str) -> list[str]:
    return [
        "ssh",
        "-i",
        str(key),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        f"root@{address}",
    ]


def _wait_test_ipv4(
    client: PveClient,
    *,
    node: str,
    vmid: int,
) -> str:
    for _ in range(120):
        try:
            interfaces = client.guest_interfaces(
                node=node,
                vmid=vmid,
                kind="vm",
            )
        except InfraManagerError:
            interfaces = None
        address = _first_ipv4(interfaces)
        if address:
            return address
        time.sleep(2)
    raise InfraManagerError(
        f"QEMU Guest Agent не сообщил IPv4; VM {vmid} "
        "оставлена для диагностики"
    )


def _parse_cloud_status(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InfraManagerError(
            "cloud-init status вернул некорректный JSON"
        ) from exc
    if not isinstance(data, dict) or data.get("status") != "done":
        raise InfraManagerError(
            "Cloud-Init завершился с неизвестной ошибкой или предупреждением"
        )
    return data


def _validate_recoverable_errors(recoverable: Any) -> None:
    if not isinstance(recoverable, dict):
        raise InfraManagerError(
            "Cloud-Init вернул неожиданный recoverable_errors"
        )
    for category, messages in recoverable.items():
        if category != "DEPRECATED" or not isinstance(messages, list):
            raise InfraManagerError(
                "Cloud-Init сообщил неизвестное предупреждение"
            )
        for message in messages:
            text = str(message)
            if (
                "of type string is deprecated in 22.2" not in text
                or "scheduled to be removed in 27.2" not in text
            ):
                raise InfraManagerError(
                    "Cloud-Init сообщил неизвестное предупреждение"
                )


def _validate_cloud_section(section: Any) -> None:
    if not isinstance(section, dict):
        raise InfraManagerError(
            "Cloud-Init вернул неожиданный формат состояния"
        )
    if section.get("errors"):
        raise InfraManagerError("Cloud-Init сообщил ошибку")
    _validate_recoverable_errors(section.get("recoverable_errors", {}))


def _validate_cloud_status(raw: str) -> None:
    data = _parse_cloud_status(raw)
    sections = (
        data,
        data.get("init", {}),
        data.get("init-local", {}),
        data.get("modules-config", {}),
        data.get("modules-final", {}),
    )
    for section in sections:
        _validate_cloud_section(section)

    console.ok(
        "Cloud-Init завершён; присутствует только известное "
        "предупреждение Proxmox о параметре user"
    )


def _validate_cloud_command(returncode: int, stdout: str) -> None:
    """Проверить код завершения и содержимое cloud-init status."""

    if returncode not in (0, 2):
        raise InfraManagerError(
            f"cloud-init status завершился с кодом {returncode}"
        )
    _validate_cloud_status(stdout)


def _prepare_test_vmid(
    client: PveClient,
    *,
    node: str,
) -> None:
    resource = client.find_vm(TEST_VMID)
    if resource is None:
        return

    actual_type = str(resource.get("type") or "")
    actual_name = str(resource.get("name") or "")
    if actual_type != "qemu" or actual_name != TEST_NAME:
        raise InfraManagerError(
            f"VMID {TEST_VMID} уже занят объектом '{actual_name}'; "
            "автоматическое удаление запрещено"
        )

    console.info(
        f"Удаление оставшегося проверочного клона {TEST_VMID}"
    )
    if client.vm_status(node=node, vmid=TEST_VMID) == "running":
        client.run_task(
            "POST",
            f"/nodes/{node}/qemu/{TEST_VMID}/status/stop",
            node=node,
        )

    config = client.vm_config(node=node, vmid=TEST_VMID)
    if is_truthy(config.get("protection")):
        client.put(
            f"/nodes/{node}/qemu/{TEST_VMID}/config",
            form={"protection": 0},
        )

    client.run_task(
        "DELETE",
        f"/nodes/{node}/qemu/{TEST_VMID}",
        node=node,
        query={"purge": 1},
    )
    if client.find_vm(TEST_VMID) is not None:
        raise InfraManagerError(
            f"Не удалось удалить старый проверочный клон {TEST_VMID}"
        )


def _delete_test_vm(
    client: PveClient,
    *,
    node: str,
) -> None:
    if client.vm_status(node=node, vmid=TEST_VMID) == "running":
        client.run_task(
            "POST",
            f"/nodes/{node}/qemu/{TEST_VMID}/status/stop",
            node=node,
        )
    config = client.vm_config(node=node, vmid=TEST_VMID)
    if is_truthy(config.get("protection")):
        client.put(
            f"/nodes/{node}/qemu/{TEST_VMID}/config",
            form={"protection": 0},
        )
    client.run_task(
        "DELETE",
        f"/nodes/{node}/qemu/{TEST_VMID}",
        node=node,
        query={"purge": 1},
    )


def run_verify_template(vmid: int) -> int:
    """Создать проверочный Full Clone шаблона и проверить гостевую ОС."""

    if vmid != 9000:
        raise InfraManagerError(
            "Пока поддерживается только шаблон 9000"
        )
    for command in ("ssh", "ssh-keygen"):
        require_command(command)

    node = os.environ.get("PACKER_NODE", "pve")
    client = PveClient.from_opentofu_env()
    _prepare_test_vmid(client, node=node)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        key = tmp_dir / "id_ed25519"
        known_hosts = tmp_dir / "known_hosts"

        run(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                str(key),
            ]
        )

        console.info(
            f"Создание проверочного Full Clone {TEST_VMID}"
        )
        client.run_task(
            "POST",
            f"/nodes/{node}/qemu/{vmid}/clone",
            node=node,
            form={
                "newid": TEST_VMID,
                "name": TEST_NAME,
                "full": 1,
                "storage": "local-lvm",
            },
            timeout=180,
        )

        public_key = key.with_suffix(".pub").read_text(
            encoding="utf-8"
        )
        encoded_key = urllib.parse.quote(public_key, safe="")
        client.put(
            f"/nodes/{node}/qemu/{TEST_VMID}/config",
            form={
                "cores": 1,
                "memory": 768,
                "protection": 0,
                "ciuser": "root",
                "ipconfig0": "ip=dhcp",
                "sshkeys": encoded_key,
            },
        )
        client.run_task(
            "POST",
            f"/nodes/{node}/qemu/{TEST_VMID}/status/start",
            node=node,
        )

        address = _wait_test_ipv4(
            client,
            node=node,
            vmid=TEST_VMID,
        )
        console.info(f"Проверочная VM получила {address}")

        ready = False
        for _ in range(60):
            attempt = run(
                [
                    "ssh",
                    "-i",
                    str(key),
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=5",
                    "-o",
                    "StrictHostKeyChecking=accept-new",
                    "-o",
                    f"UserKnownHostsFile={known_hosts}",
                    f"root@{address}",
                    "true",
                ],
                check=False,
                capture_output=True,
            )
            if attempt.returncode == 0:
                ready = True
                break
            time.sleep(2)
        if not ready:
            raise InfraManagerError(
                f"SSH проверочного клона {TEST_VMID} не стал "
                "доступен; VM оставлена для диагностики"
            )

        ssh = _ssh_base(key, known_hosts, address)
        cloud = run(
            [
                *ssh,
                "cloud-init status --wait --format json",
            ],
            check=False,
            capture_output=True,
        )
        _validate_cloud_command(cloud.returncode, cloud.stdout)

        agent = run(
            [*ssh, "systemctl is-active --quiet qemu-guest-agent"],
            check=False,
        )
        if agent.returncode != 0:
            raise InfraManagerError(
                f"QEMU Guest Agent не активен; VM {TEST_VMID} "
                "оставлена для диагностики"
            )
        console.ok("QEMU Guest Agent активен")

        machine_id = run(
            [*ssh, "test -s /etc/machine-id"],
            check=False,
        )
        if machine_id.returncode != 0:
            raise InfraManagerError(
                f"/etc/machine-id отсутствует или пуст; VM "
                f"{TEST_VMID} оставлена для диагностики"
            )
        console.ok("machine-id создан")

        host_key = run(
            [*ssh, "test -s /etc/ssh/ssh_host_ed25519_key"],
            check=False,
        )
        if host_key.returncode != 0:
            raise InfraManagerError(
                f"SSH host keys не созданы; VM {TEST_VMID} "
                "оставлена для диагностики"
            )
        console.ok("SSH host keys созданы")

        _delete_test_vm(client, node=node)

    console.ok(f"Full Clone шаблона {vmid} успешно проверен")
    return 0
