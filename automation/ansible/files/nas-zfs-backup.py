#!/usr/bin/env python3
"""Локальная безопасная передача снимков NAS на отдельный пул ZFS.

Никогда не удаляет снимки, не выполняет zfs receive -F и не выбирает
чужие пулы. Работает на NAS независимо от доступности PVE.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import subprocess
import sys
from pathlib import Path

CONFIG = Path("/etc/nas-apps/zfs-backup.json")
LOCK = Path("/run/lock/nas-zfs-backup.lock")


class BackupError(RuntimeError):
    """Безопасный отказ копирования без разрушительных исправлений."""


def execute(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Запустить разрешённую команду ZFS без выполнения через оболочку."""

    result = subprocess.run(list(args), check=False, text=True, capture_output=True)
    if check and result.returncode:
        raise BackupError(
            f"Команда ZFS не выполнена: {args[0]} {args[1]} "
            f"(код {result.returncode})"
        )
    return result


def verify_pool(name: str, expected_guid: str) -> None:
    """Проверить, что ожидаемый физический пул не был подменён."""

    result = execute("zpool", "get", "-H", "-o", "value", "guid", name)
    if result.stdout.strip() != expected_guid:
        raise BackupError(f"GUID пула {name} не соответствует конфигурации")


def load_configuration() -> dict[str, str]:
    """Не разрешить переопределить источник или приёмник копирования."""

    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError("Конфигурация резервирования отсутствует или повреждена") from exc
    if not isinstance(data, dict) or set(data) != {
        "source_pool", "source_guid", "backup_pool", "backup_guid"
    }:
        raise BackupError("В конфигурации резервирования неизвестные поля")
    if data.get("source_pool") != "nas-data" or data.get("backup_pool") != "nas-backup":
        raise BackupError("Разрешены только раздельные пулы nas-data и nas-backup")
    for field in ("source_guid", "backup_guid"):
        value = data[field]
        if not isinstance(value, str) or not value.isdecimal() or len(value) > 20:
            raise BackupError(f"Нет подтверждённого GUID: {field}")
    if data["source_guid"] == data["backup_guid"]:
        raise BackupError("Основной и резервный пулы не могут совпадать")
    return data


def snapshots(dataset: str) -> set[str]:
    """Получить снимки только заданного датасета, без потомков."""

    result = execute("zfs", "list", "-H", "-t", "snapshot", "-o", "name", "-r", dataset)
    prefix = dataset + "@infra-"
    return {
        item.removeprefix(dataset + "@") for item in result.stdout.splitlines()
        if item.startswith(prefix)
    }


def common_snapshot(source: str, target: str) -> str | None:
    """Найти последний общий снимок перед безопасным приращением."""

    common = snapshots(source) & snapshots(target)
    return max(common) if common else None


def send_replica(source: str, target: str, latest: str, previous: str | None) -> None:
    """Передать поток ZFS без промежуточного хранения и без принудительного отката."""

    src_new = f"{source}@{latest}"
    send_args = ["zfs", "send", "-R"]
    if previous:
        send_args += ["-I", f"{source}@{previous}"]
    send_args.append(src_new)
    producer = subprocess.Popen(
        send_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if producer.stdout is None:
        raise BackupError("Не удалось получить поток ZFS")
    try:
        consumer = subprocess.Popen(
            ["zfs", "receive", "-u", target],
            stdin=producer.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        producer.stdout.close()
        _out, receiver_err = consumer.communicate()
        _out, sender_err = producer.communicate()
        if producer.returncode or consumer.returncode:
            raise BackupError(
                "Передача ZFS не завершена. Промежуточный приём не очищен; "
                "проверьте журнал и состояние пула вручную. "
                f"Коды отправителя/приёмника: {producer.returncode}/{consumer.returncode}"
            )
    finally:
        if producer.poll() is None:
            producer.kill()
            producer.wait()


def perform_backup() -> None:
    """Проверить конфигурацию до снимка и выполнить только разрешённую передачу."""

    config = load_configuration()
    source, target_pool = config["source_pool"], config["backup_pool"]
    target = target_pool + "/replica"
    verify_pool(source, config["source_guid"])
    verify_pool(target_pool, config["backup_guid"])
    target_status = execute(
        "zfs", "list", "-H", "-o", "name", target, check=False,
    )
    exists = target_status.returncode == 0
    prior = common_snapshot(source, target) if exists else None
    if exists and prior is None:
        raise BackupError(
            "Резервный датасет существует без общего снимка; "
            "автоматическая перезапись запрещена"
        )
    label = "infra-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    execute("zfs", "snapshot", "-r", f"{source}@{label}")
    send_replica(source, target, label, prior)
    print(f"[ОК] Снимок {label} передан в {target} без удаления старых данных")


def main() -> int:
    """Удерживать исключительную блокировку на всё время передачи."""

    try:
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        with LOCK.open("w", encoding="utf-8") as guard:
            fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            perform_backup()
    except (OSError, BackupError) as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
