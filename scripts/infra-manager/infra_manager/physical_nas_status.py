"""Physical NAS status using common declarative status.yaml and service fields.

Disk identity and ZFS GUID remain fail-closed in the existing host preflight.
This module checks hardware health then delegates service checks to the SAME
generic executor as guest status. It contains no application check list.
"""
from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Iterable

from .physical_storage import DiskInspection
from .physical_nas_volumes import VerifiedVolume
from .common import InfraManagerError, console, run
from .physical_host_ssh import PhysicalHostSshSession
from .guest_status import load_declared_checks, _run_declared_check, _service_fields
from .guest_deploy import project_branch_for_checkout, project_revision


def verify_nas_services(
    session: PhysicalHostSshSession,
    *, repo_root: Path, host_name: str,
    disks: Iterable[DiskInspection] = (),
    volumes: Iterable[VerifiedVolume] = (),
) -> None:
    """Check hardware and execute common YAML service checks remotely."""
    for disk in disks:
        report = run(
            session.command("smartctl --json -H " + shlex.quote(disk.path)),
            check=False, capture_output=True,
        )
        try:
            output = json.loads(report.stdout)
        except json.JSONDecodeError:
            console.warning(f"Нет сведений SMART: {disk.model}, {disk.serial}")
            continue
        status = output.get("smart_status") if isinstance(output, dict) else None
        if isinstance(status, dict) and status.get("passed") is False:
            raise InfraManagerError(
                f"SMART предупреждает о неисправности диска: {disk.model}, {disk.serial}"
            )
        if not isinstance(status, dict) or status.get("passed") is not True:
            console.warning(f"Не удалось подтвердить SMART: {disk.model}, {disk.serial}")
        else:
            console.ok(f"SMART: {disk.model}, {disk.serial or 'без серийного номера'}")

    for volume in volumes:
        result = run(
            session.command("df -P " + shlex.quote(volume.mountpoint)),
            check=False, capture_output=True,
        )
        if result.returncode:
            raise InfraManagerError(
                f"Не удалось проверить свободное место на {volume.mountpoint}"
            )
        lines = result.stdout.splitlines()
        if len(lines) < 2:
            raise InfraManagerError("Неизвестный формат свободного места на NAS")
        fields = lines[-1].split()
        if len(fields) < 6 or not fields[4].endswith("%"):
            raise InfraManagerError("Не удалось прочитать процент заполнения NAS")
        try:
            percentage = int(fields[4].rstrip("%"))
        except ValueError as exc:
            raise InfraManagerError("Некорректный процент заполнения NAS") from exc
        if percentage >= 95:
            console.warning(f"Мало места на томе {volume.role}: занято {percentage}%")
        else:
            console.ok(f"Том {volume.role}: занято {percentage}%")


    path = repo_root / "infrastructure" / "hosts" / host_name / "status.yaml"
    checks = load_declared_checks(
        path, identity_field="host", identity_value=host_name,
    )

    def execute(_node: str, _address: str, argv: list[str], *,
                capture_output: bool = True):
        return run(
            session.command(shlex.join(argv)),
            check=True, capture_output=capture_output,
        )

    for item in checks:
        _run_declared_check(
            "", session.host.address or "", item,
            remote_executor=execute,
        )
        console.ok(str(item["message"]))


def _omv_url(session: PhysicalHostSshSession, address: str) -> str:
    """Use actual OMV webadmin settings; never assume port 80 is configured."""
    response = run(
        session.command("omv-confdbadm read conf.system.webadmin"),
        check=False, capture_output=True,
    )
    if response.returncode:
        return "не удалось определить из OpenMediaVault"
    try:
        settings = json.loads(response.stdout)
    except (ValueError, TypeError):
        return "не удалось определить из OpenMediaVault"
    if not isinstance(settings, dict):
        return "не удалось определить из OpenMediaVault"
    use_ssl = settings.get("enablessl") is True
    port = settings.get("sslport" if use_ssl else "port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return "не удалось определить из OpenMediaVault"
    scheme = "https" if use_ssl else "http"
    return f"{scheme}://{address}:{port}/"


def _host_password(session: PhysicalHostSshSession, spec: dict[str, str]) -> str:
    """Only this explicitly managed root-only credential is publishable."""
    if spec != {
        "source": "host_file",
        "path": "/etc/nas-apps/urbackup/admin-password",
    }:
        raise InfraManagerError("Запрещённый источник учётных данных физического сервера")
    result = run(
        session.command("cat -- /etc/nas-apps/urbackup/admin-password"),
        capture_output=True, check=False,
    )
    value = result.stdout.strip()
    if result.returncode or not value or "\n" in value:
        return "пароль не доступен через доверенный SSH"
    return value


def show_nas_status(
    repo_root: Path, session: PhysicalHostSshSession,
    provision: dict, *, project_revision: str | None = None,
    show_secrets: bool = False,
) -> None:
    """Print the same operator-facing sections as show_guest_status()."""
    address = session.host.address
    if not address:
        raise InfraManagerError("Не определён адрес физического сервера")
    services = provision["services"]
    separator = "=" * 60
    print("\n" + separator)
    print(f"  Физический сервер — {session.host.name}")
    print(separator)
    print("\nСистема")
    print(f"  {'Тип:':<12}Физический сервер")
    print(f"  {'ОС:':<12}Debian 13 / OpenMediaVault 8")
    print(f"  {'Адрес:':<12}{address}")

    print("\nПроект")
    print(f"  {'Ветка:':<12}{project_branch_for_checkout(repo_root)}")
    print(f"  {'Версия:':<12}{project_revision or globals()['project_revision'](repo_root)}")
    for name in ("openmediavault", "qbittorrent", "urbackup"):
        spec = services[name]
        url = (
            _omv_url(session, address)
            if spec.get("address") == "auto_omv"
            else spec["url"].replace("{address}", address)
        )
        svc = {"name": spec["title"], "url": url,
               "operator": spec.get("operator", {})}
        fields = _service_fields(
            "", address, svc, show_secrets=show_secrets,
            password_resolver=lambda credential: _host_password(session, credential),
        )
        if "access" in spec:
            fields.append(("Доступ", spec["access"]))
        if "password" not in spec.get("operator", {}):
            fields.append(("Пароль", "не хранится в конфигурации проекта"))
        width = max(len(k) for k, _ in fields) + 3
        print("\n" + spec["title"])
        for label, value in fields:
            print(f"  {label + ':':<{width}}{value}")

    print("\n" + separator)
    print("  NAS готов")
    print(separator)
