"""Проверка служб физического NAS после безопасной сверки томов."""

from __future__ import annotations

import json
import shlex
from typing import Iterable

from .physical_storage import DiskInspection
from .physical_nas_volumes import VerifiedVolume
from .common import InfraManagerError, console, run
from .physical_host_ssh import PhysicalHostSshSession


def verify_nas_services(
    session: PhysicalHostSshSession,
    *, disks: Iterable[DiskInspection] = (),
    volumes: Iterable[VerifiedVolume] = (),
) -> None:
    """Проверить диски и службы; запуск сервера не доказывает наличие копий."""

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

    critical = {
        "docker.service",
        "nas-urbackup.service",
        "nas-urbackup-client.service",
        "nas-urbackup-local.timer",
    }
    for name in (
        "docker.service",
        "smbd.service",
        "nas-qbittorrent.service",
        "nas-urbackup.service",
        "urbackupclientbackend.service",
        "nas-urbackup-local.timer",
        "nas-urbackup-recovery.timer",
    ):
        result = run(
            session.command("systemctl is-active " + name),
            check=False, capture_output=True,
        )
        if result.returncode or result.stdout.strip() != "active":
            if name in critical:
                raise InfraManagerError(f"Служба NAS не запущена: {name}")
            console.warning(f"Необязательная служба NAS не запущена: {name}")
            continue
        console.ok(f"Служба {name}: работает")

    container = run(
        session.command(
            "docker inspect --format '{{.State.Running}}' nas-qbittorrent"
        ),
        check=False, capture_output=True,
    )
    if container.returncode or container.stdout.strip() != "true":
        console.warning("Контейнер qBittorrent не запущен (не влияет на UrBackup)")

    server = run(
        session.command("docker inspect --format '{{.State.Running}}' nas-urbackup"),
        check=False, capture_output=True,
    )
    if server.returncode or server.stdout.strip() != "true":
        raise InfraManagerError("UrBackup Server на NAS не работает")
    console.ok("UrBackup Server: контейнер запущен")

    client = run(
        session.command("docker inspect --format '{{.State.Running}}' nas-urbackup-client"),
        check=False, capture_output=True,
    )
    if client.returncode or client.stdout.strip() != "true":
        raise InfraManagerError("UrBackup Client Docker на NAS не работает")
    console.ok("UrBackup Client Docker: контейнер запущен")

    safety = run(
        session.command("/usr/local/sbin/nas-urbackup-safe status"),
        check=False, capture_output=True,
    )
    if safety.returncode:
        raise InfraManagerError("UrBackup: диски или настройка администратора не подтверждены")
    console.ok("UrBackup: GUID резервного ZFS и доступ подтверждены")
    recent = run(
        session.command("systemctl show -p Result --value nas-urbackup-local.service"),
        check=False, capture_output=True,
    )
    started = run(
        session.command("systemctl show -p ExecMainStartTimestamp --value nas-urbackup-local.service"),
        check=False, capture_output=True,
    )
    if (recent.returncode or recent.stdout.strip() != "success" or
            started.returncode or started.stdout.strip() in ("", "n/a")):
        console.warning("Локальное файловое резервирование NAS ещё не подтверждено")
    else:
        console.ok("Локальное задание NAS завершилось успешно (проверка восстановления отдельно)")
    console.warning("История резервных копий UrBackup и пробное восстановление "
                    "не подтверждаются одним только запуском сервера")

    for pool in ("nas-data", "nas-backup"):
        result = run(
            session.command("zpool list -H -o health " + pool),
            check=False, capture_output=True,
        )
        if result.returncode or result.stdout.strip() != "ONLINE":
            raise InfraManagerError(f"Пул ZFS {pool} не в состоянии ONLINE")
        console.ok(f"Пул {pool}: ONLINE")

    # Старые ZFS-снимки больше не являются критерием готовности UrBackup.
    # Подробный статус файловых копий будет основан на API/истории сервера.
