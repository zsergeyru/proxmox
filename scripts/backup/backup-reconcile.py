#!/usr/bin/env python3
"""Внутреннее согласование резервирования PVE по storage.yaml и backup.yaml.

Вызывается при deploy 910 и службой PVE, не является пользовательской
командой. Неизвестные носители не форматирует и не подменяет локальной ФС.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any

import yaml

BASE = Path(__file__).resolve().parent
DESIRED = Path("/usr/local/share/infra-manager/backup.yaml.default")
DEVICES = Path("/usr/local/share/infra-manager/storage-pve.yaml.default")
CURRENT = Path("/etc/infra-manager/backup/backup.yaml")
REPORT = Path("/var/lib/infra-manager/backup/reconcile.json")


def module(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, BASE / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Отсутствует служебный модуль {filename}")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def command(*argv: str) -> str:
    result = subprocess.run(
        list(argv), text=True, capture_output=True, check=False, timeout=90,
    )
    if result.returncode:
        raise RuntimeError(f"Не завершена проверка {argv[0]}")
    return result.stdout.strip()


def load_yaml(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Нет проверенного машинного описания резервирования")
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise RuntimeError("Некорректное описание дисков или резервирования")
    return value


def save_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".backup-config-", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            yaml.safe_dump(payload, output, sort_keys=False, allow_unicode=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def reconcile_config() -> tuple[dict[str, Any], dict[str, Any]]:
    """Применить желаемую политику, сохраняя подтверждённые локальные UUID."""

    desired = load_yaml(DESIRED)
    devices = load_yaml(DEVICES)
    if desired.get("host") != "pve" or devices.get("host") != "pve":
        raise RuntimeError("Описание резервирования принадлежит другому серверу")
    old = load_yaml(CURRENT) if CURRENT.exists() else None
    if old is not None:
        old_targets = {
            entry["id"]: entry for entry in old.get("destinations", [])
            if isinstance(entry, dict) and isinstance(entry.get("id"), str)
        }
        for target in desired["destinations"]:
            previous = old_targets.get(target["id"])
            if previous is None:
                continue
            for key in ("uuid", "source"):
                existing = previous.get(key)
                intended = target.get(key)
                if intended and existing and intended != existing:
                    raise RuntimeError(
                        f"Назначение {target['id']} изменилось: нужно "
                        "подтверждение нового носителя"
                    )
                if not intended and existing:
                    target[key] = existing
    # Хранить наблюдаемое состояние отдельно от исходного Git-файла.
    if old != desired:
        save_yaml(CURRENT, desired)
    return desired, devices


def device_identity(storage: dict[str, Any]) -> tuple[Path, str] | None:
    listed = [
        entry for entry in storage.get("devices", [])
        if isinstance(entry, dict) and entry.get("id") == "backup_ssd"
        and entry.get("role") == "backup"
    ]
    if len(listed) != 1:
        raise RuntimeError("В storage.yaml не найден один резервный SSD")
    entry = listed[0]
    serial, by_id = entry.get("serial"), entry.get("by_id")
    if not serial or not by_id:
        return None
    if not isinstance(serial, str) or not isinstance(by_id, str):
        raise RuntimeError("Неверная идентичность резервного SSD")
    path = Path(by_id)
    if not by_id.startswith("/dev/disk/by-id/") or path.name.endswith("-part1"):
        raise RuntimeError("Резервный SSD должен быть указан через /dev/disk/by-id")
    return path, serial


def prepare_ssd(config: dict[str, Any], storage: dict[str, Any]) -> None:
    """Безопасная работа только с указанным в Git резервным SSD."""

    identity = device_identity(storage)
    if identity is None:
        raise RuntimeError(
            "Нет подтверждения SSD: заполните serial и by_id в storage.yaml"
        )
    path, serial = identity
    device = module("backup-device.py", "infra_backup_device")
    mount = module("backup-mount.py", "infra_backup_mount")
    partition = Path(str(path) + "-part1")
    if partition.is_symlink():
        uuid = command("blkid", "-s", "UUID", "-o", "value", str(partition))
        mount.configure_fast(partition, serial, uuid)
        return
    approved_serial = config.get("initialize_blank_ssd_serial")
    if approved_serial != serial:
        raise RuntimeError(
            "Резервный SSD не подготовлен. Разметка возможна только после "
            "подтверждения его серийного номера в backup.yaml"
        )
    _, size = device.empty_disk(path, serial)
    device.prepare(path, serial, device.confirmation(path, serial, size))
    uuid = command("blkid", "-s", "UUID", "-o", "value", str(partition))
    mount.configure_fast(partition, serial, uuid)


def prepare_nas(config: dict[str, Any]) -> None:
    source = next(
        (d.get("source") for d in config["destinations"]
         if d["id"] == "nas-backup"),
        None,
    )
    if not source:
        raise RuntimeError(
            "Нет подключения NAS: укажите проверенный экспорт NFSv4 в backup.yaml"
        )
    mount = module("backup-mount.py", "infra_backup_mount_nas")
    mount.configure_nas(source)


def ready(config: dict[str, Any]) -> None:
    """Создать и включить резервирование только после всех проверок."""

    core = module("backup-pve.py", "infra_backup_core")
    setup = module("backup-setup.py", "infra_backup_setup")
    # До действий с root token необходимо подтвердить оба носителя.
    core.check_destinations(config["destinations"], allow_uninitialized=True)
    credential = Path(config["openbao"]["approle_file"])
    if not credential.is_file():
        command(
            "/usr/local/sbin/infra-manager-openbao-unseal",
            "--configure-backup-access",
        )
    setup.initialize()
    key_dest = config.get("recovery_key_destination")
    if not setup.CONFIRMATION.is_file():
        if not isinstance(key_dest, str) or not key_dest:
            raise RuntimeError(
                "Нет независимого ключа: укажите recovery_key_destination "
                "в backup.yaml для третьего проверенного носителя"
            )
        setup.export_key(Path(key_dest))
    if not core.LAST_SUCCESS.is_file():
        core.backup(config, execute=True)
    setup.enable()


def save_report(state: str, reason: str) -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".backup-report-", dir=REPORT.parent)
    temporary = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump({
                "state": state,
                "reason": reason,
                "checked_utc": datetime.now(timezone.utc).isoformat(),
            }, output, ensure_ascii=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, REPORT)
    finally:
        temporary.unlink(missing_ok=True)


def status() -> None:
    """Краткое состояние для штатного infra-manager status 910."""

    if not REPORT.is_file():
        print("[ПРЕДУПРЕЖДЕНИЕ] Резервирование: ещё не проверено")
        return
    try:
        report = json.loads(REPORT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("[ПРЕДУПРЕЖДЕНИЕ] Резервирование: состояние недоступно")
        return
    if report.get("state") == "ready":
        print("[ОК] Резервирование PVE/910/HAOS настроено; последняя копия проверяется по журналу")
    else:
        reason = str(report.get("reason") or "ожидается подготовка")[:240]
        print(f"[ПРЕДУПРЕЖДЕНИЕ] Резервирование: {reason}")


def reconcile() -> None:
    """Идемпотентная процедура: сбой носителя не делает 910 неработоспособным."""

    config, storage = reconcile_config()
    try:
        prepare_ssd(config, storage)
        prepare_nas(config)
        # mount-операции меняют локальные обнаруженные UUID.
        config = load_yaml(CURRENT)
        ready(config)
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        save_report("pending", str(exc))
        print(f"[ПРЕДУПРЕЖДЕНИЕ] Резервирование ожидает подготовки: {exc}")
        return
    save_report("ready", "Оба хранилища подтверждены, копирование включено")
    print("[ОК] Резервирование PVE, 910 и VM 100 подключено к расписанию")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("ОШИБКА: внутренняя проверка запускается только root на PVE")
    try:
        if args.status:
            status()
        else:
            reconcile()
    except (RuntimeError, OSError, ValueError, KeyError, yaml.YAMLError) as exc:
        # Не печатать закрытые поля настроек или содержимое запроса OpenBao.
        save_report("pending", "Ошибка описания или проверки резервирования")
        print(f"[ПРЕДУПРЕЖДЕНИЕ] Резервирование не настроено: {type(exc).__name__}")
        return 0  # 910 не должен падать из-за ещё не введённого NAS.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
