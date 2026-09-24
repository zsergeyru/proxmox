"""Сборка и проверка Packer-шаблонов Proxmox."""

from __future__ import annotations

import json
import os
import re
import secrets
import ssl
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .common import InfraManagerError, console, require_command, run
from .pve import CA_BUNDLE, PveClient

ISO_BASE = "https://cdimage.debian.org/debian-cd/current/amd64/iso-cd"
TEMPLATE_VERSION = 8
TEST_VMID = 9099
TEST_NAME = "smoke-template-9000"


def _tool_env() -> dict[str, str]:
    if not CA_BUNDLE.is_file() or CA_BUNDLE.stat().st_size == 0:
        raise InfraManagerError(f"Не найден CA bundle: {CA_BUNDLE}")
    env = os.environ.copy()
    env["SSL_CERT_FILE"] = str(CA_BUNDLE)
    return env


def _truthy(value: Any) -> bool:
    return value in (1, True, "1", "true")


def _template_failures(config: dict[str, Any]) -> list[str]:
    failures: list[str] = []

    if not _truthy(config.get("template", 0)):
        failures.append(f"template={config.get('template', '<отсутствует>')}")
    if config.get("name") != "tpl-debian13":
        failures.append(f"name={config.get('name', '<отсутствует>')}")
    if f"template-version={TEMPLATE_VERSION}" not in str(
        config.get("description") or ""
    ):
        failures.append(
            f"description={config.get('description', '<отсутствует>')}"
        )
    if not _truthy(config.get("protection", 0)):
        failures.append(
            f"protection={config.get('protection', '<отсутствует>')}"
        )
    if config.get("scsihw") != "virtio-scsi-single":
        failures.append(
            f"scsihw={config.get('scsihw', '<отсутствует>')}"
        )

    agent = str(config.get("agent") or "")
    if not (
        agent == "1"
        or agent.startswith("1,")
        or re.search(r"(^|,)enabled=1($|,)", agent)
    ):
        failures.append(f"agent={agent or '<отсутствует>'}")

    if "cloudinit" not in str(config.get("ide0") or ""):
        failures.append("ide0=<cloudinit отсутствует>")
    if config.get("ciuser") != "root":
        failures.append(
            f"ciuser={config.get('ciuser', '<отсутствует>')}"
        )
    if "ip=dhcp" not in str(config.get("ipconfig0") or ""):
        failures.append(
            f"ipconfig0={config.get('ipconfig0', '<отсутствует>')}"
        )
    if str(config.get("ciupgrade", 0)).lower() not in {"0", "false"}:
        failures.append(f"ciupgrade={config.get('ciupgrade')}")

    return failures


def verify_template_contract(
    client: PveClient,
    *,
    node: str,
    vmid: int,
) -> None:
    config = client.vm_config(node=node, vmid=vmid)
    failures = _template_failures(config)
    if failures:
        details = "\n".join(f"  - {item}" for item in failures)
        raise InfraManagerError(
            f"Шаблон {vmid} не соответствует базовому контракту:\n"
            f"{details}\n"
            f"Фактическая конфигурация: "
            f"{json.dumps(config, ensure_ascii=False, sort_keys=True)}"
        )
    console.ok(f"Контракт шаблона {vmid} подтверждён")


def _finalize_template(
    client: PveClient,
    *,
    node: str,
    vmid: int,
) -> None:
    console.info("Финальная конфигурация Cloud-Init и защиты")
    client.put(
        f"/nodes/{node}/qemu/{vmid}/config",
        form={
            "ciuser": "root",
            "ciupgrade": 0,
            "ipconfig0": "ip=dhcp",
            "protection": 1,
        },
    )
    verify_template_contract(client, node=node, vmid=vmid)


def _existing_template_is_compatible(
    client: PveClient,
    *,
    node: str,
    vmid: int,
) -> bool:
    resource = client.find_vm(vmid)
    if resource is None:
        return False

    if resource.get("type") != "qemu":
        raise InfraManagerError(
            f"VMID {vmid} уже занят LXC или другим объектом"
        )

    config = client.vm_config(node=node, vmid=vmid)
    name = str(config.get("name") or "")
    description = str(config.get("description") or "")

    if (
        _truthy(config.get("template"))
        and name == "tpl-debian13"
        and f"template-version={TEMPLATE_VERSION}" in description
    ):
        return True

    if name == f"builder-{vmid}":
        raise InfraManagerError(
            f"VMID {vmid} содержит незавершённую VM-сборщик. "
            "Она оставлена для диагностики."
        )

    raise InfraManagerError(
        f"VMID {vmid} уже занят несовместимым объектом. "
        "Автоматическая замена запрещена."
    )


def _resolve_debian_iso() -> tuple[str, str]:
    console.info("Определение актуального Debian 13 netinst ISO")
    request = urllib.request.Request(
        f"{ISO_BASE}/SHA512SUMS",
        headers={"Accept": "text/plain"},
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=60,
            context=ssl.create_default_context(),
        ) as response:
            sums = response.read().decode("utf-8")
    except (OSError, urllib.error.URLError) as exc:
        raise InfraManagerError(
            f"Не удалось получить SHA512SUMS Debian: {exc}"
        ) from exc

    pattern = re.compile(
        r"^([0-9a-fA-F]{128})\s+"
        r"(debian-13(?:[.]\d+)+-amd64-netinst[.]iso)$",
        re.MULTILINE,
    )
    matches = pattern.findall(sums)
    if not matches:
        raise InfraManagerError(
            "В current Debian не найден Debian 13 amd64 netinst ISO"
        )

    checksum, filename = matches[-1]
    console.info(f"Источник: {filename}")
    return f"{ISO_BASE}/{filename}", f"sha512:{checksum}"


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


def _validate_cloud_status(raw: str) -> None:
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

    sections = [
        data,
        data.get("init", {}),
        data.get("init-local", {}),
        data.get("modules-config", {}),
        data.get("modules-final", {}),
    ]
    for section in sections:
        if not isinstance(section, dict):
            raise InfraManagerError(
                "Cloud-Init вернул неожиданный формат состояния"
            )
        if section.get("errors"):
            raise InfraManagerError(
                "Cloud-Init сообщил ошибку"
            )
        recoverable = section.get("recoverable_errors", {})
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

    console.ok(
        "Cloud-Init завершён; присутствует только известное "
        "предупреждение Proxmox о параметре user"
    )


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
    if _truthy(config.get("protection")):
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
    if _truthy(config.get("protection")):
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
            capture_output=True,
        )
        _validate_cloud_status(cloud.stdout)

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


def run_build_template(repo_root: Path, vmid: int) -> int:
    """Собрать и проверить Packer-шаблон."""

    if vmid != 9000:
        raise InfraManagerError(
            "Пока поддерживается только шаблон 9000"
        )
    require_command("packer")

    packer_dir = repo_root / "automation" / "packer" / str(vmid)
    if not packer_dir.is_dir():
        raise InfraManagerError(f"Не найден каталог {packer_dir}")

    node = os.environ.get("PACKER_NODE", "pve")
    client = PveClient.from_opentofu_env()
    env = _tool_env()

    if _existing_template_is_compatible(
        client,
        node=node,
        vmid=vmid,
    ):
        _finalize_template(client, node=node, vmid=vmid)
        run_verify_template(vmid)
        console.ok(
            f"Шаблон {vmid} уже соответствует версии "
            f"{TEMPLATE_VERSION} и успешно проверен; сборка не требуется"
        )
        return 0

    iso_url, iso_checksum = _resolve_debian_iso()
    build_password = secrets.token_hex(24)

    env["PKR_VAR_proxmox_token"] = client.token_secret
    env["PKR_VAR_build_password"] = build_password

    common_vars = [
        f"-var=proxmox_url={client.url}/api2/json",
        f"-var=proxmox_username={client.token_id}",
        f"-var=node={node}",
        f"-var=iso_url={iso_url}",
        f"-var=iso_checksum={iso_checksum}",
    ]

    console.info("Проверка Packer")
    run(["packer", "init", str(packer_dir)], env=env)
    run(
        ["packer", "validate", *common_vars, str(packer_dir)],
        env=env,
    )

    console.info(f"Сборка шаблона {vmid}")
    run(
        ["packer", "build", *common_vars, str(packer_dir)],
        env=env,
    )

    _finalize_template(client, node=node, vmid=vmid)
    run_verify_template(vmid)
    console.ok(f"Шаблон {vmid} полностью собран и проверен")
    return 0
