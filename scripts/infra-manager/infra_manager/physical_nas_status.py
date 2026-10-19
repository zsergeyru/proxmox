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
    """Проверить готовность ZFS, Samba, Docker и расписания копирования."""

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

    for name in (
        "docker.service",
        "smbd.service",
        "nas-qbittorrent.service",
        "nas-zfs-backup.timer",
    ):
        result = run(
            session.command("systemctl is-active " + name),
            check=False, capture_output=True,
        )
        if result.returncode or result.stdout.strip() != "active":
            raise InfraManagerError(f"Служба NAS не запущена: {name}")
        console.ok(f"Служба {name}: работает")

    container = run(
        session.command(
            "docker inspect --format '{{.State.Running}}' nas-qbittorrent"
        ),
        check=False, capture_output=True,
    )
    if container.returncode or container.stdout.strip() != "true":
        raise InfraManagerError("Контейнер qBittorrent не работает")

    for pool in ("nas-data", "nas-backup"):
        result = run(
            session.command("zpool list -H -o health " + pool),
            check=False, capture_output=True,
        )
        if result.returncode or result.stdout.strip() != "ONLINE":
            raise InfraManagerError(f"Пул ZFS {pool} не в состоянии ONLINE")
        console.ok(f"Пул {pool}: ONLINE")

    snapshots = run(
        session.command(
            "zfs list -H -t snapshot -o name -r nas-backup/replica"
        ),
        check=False, capture_output=True,
    )
    if snapshots.returncode:
        console.warning(
            "Резервная копия ещё не обнаружена: проверьте первый запуск "
            "nas-zfs-backup.timer"
        )
    else:
        lines = [
            item for item in snapshots.stdout.splitlines()
            if item.startswith("nas-backup/replica@infra-")
        ]
        if not lines:
            console.warning("Пока нет снимков ZFS, переданных в nas-backup")
        else:
            console.ok(f"Сохранены снимки на резервном пуле: {len(lines)}")
