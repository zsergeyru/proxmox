"""Сборка Packer-шаблона Proxmox."""

from __future__ import annotations

import os
import re
import secrets
import ssl
import urllib.error
import urllib.request
from pathlib import Path

from .common import InfraManagerError, console, require_command, run
from .pve import CA_BUNDLE, PveClient
from .template import TEMPLATE_VERSION, is_truthy, verify_template_contract
from .template_verify import run_verify_template

ISO_BASE = "https://cdimage.debian.org/debian-cd/current/amd64/iso-cd"


def _tool_env() -> dict[str, str]:
    if not CA_BUNDLE.is_file() or CA_BUNDLE.stat().st_size == 0:
        raise InfraManagerError(f"Не найден CA bundle: {CA_BUNDLE}")
    env = os.environ.copy()
    env["SSL_CERT_FILE"] = str(CA_BUNDLE)
    return env


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
        is_truthy(config.get("template"))
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


def _packer_inputs(
    env: dict[str, str],
    *,
    client: PveClient,
    node: str,
    iso_url: str,
    iso_checksum: str,
    build_password: str,
) -> tuple[dict[str, str], list[str]]:
    """Подготовить окружение и несекретные аргументы Packer."""
    packer_env = dict(env)
    packer_env["PKR_VAR_proxmox_token"] = client.token_secret
    packer_env["PKR_VAR_build_password"] = build_password
    common_vars = [
        f"-var=proxmox_url={client.url}/api2/json",
        f"-var=proxmox_username={client.token_id}",
        f"-var=node={node}",
        f"-var=iso_url={iso_url}",
        f"-var=iso_checksum={iso_checksum}",
    ]
    return packer_env, common_vars


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

    env, common_vars = _packer_inputs(
        env,
        client=client,
        node=node,
        iso_url=iso_url,
        iso_checksum=iso_checksum,
        build_password=build_password,
    )

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
