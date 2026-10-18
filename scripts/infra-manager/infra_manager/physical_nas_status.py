"""Проверка служб физического NAS после безопасной сверки томов."""

from __future__ import annotations

from .common import InfraManagerError, console, run
from .physical_host_ssh import PhysicalHostSshSession


def verify_nas_services(session: PhysicalHostSshSession) -> None:
    """Проверить готовность ZFS, Samba, Docker и расписания копирования."""

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
