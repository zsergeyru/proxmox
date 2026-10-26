"""Approval-free, preserve-only HDD admission for a physical host deploy.

On PVE this is a transport shim: Ansible owns all volume inspection, SSH
trust and one-time controller identity. It can never approve formatting.
"""
from __future__ import annotations

import json
import re
import sys


PREFIX = "PHYSICAL_STORAGE_PLAN_JSON="
_SERIAL = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")


def confirm_nas_storage(name: str, forward, error_type: type[Exception]) -> int:
    """Forward only a verified preservation fingerprint, never create intent."""

    result = forward(["deploy", name, "--_physical_stage", "plan"], capture_output=True)
    if result.stdout:
        for line in result.stdout.splitlines():
            if not line.startswith(PREFIX):
                print(line)
    if result.returncode:
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        return result.returncode

    matches = [
        line.removeprefix(PREFIX)
        for line in result.stdout.splitlines() if line.startswith(PREFIX)
    ]
    if len(matches) != 1:
        raise error_type("NAS не вернул один однозначный план сохранения HDD")
    try:
        plan = json.loads(matches[0])
    except (ValueError, TypeError) as exc:
        raise error_type("Неверный формат подтверждения существующих HDD") from exc
    if not isinstance(plan, dict):
        raise error_type("Не получено описание физического хранилища NAS")
    fingerprint = plan.get("fingerprint")
    items = plan.get("items")
    if not isinstance(fingerprint, str) or not _SHA.fullmatch(fingerprint):
        raise error_type("Не подтверждён отпечаток физических дисков")
    if (
        plan.get("host") not in (None, name)
        or plan.get("requires_creation") is not False
        or not isinstance(items, list)
        or len(items) != 4
        or not all(isinstance(item, dict) for item in items)
        or [item.get("role") for item in items]
        != ["system", "shared", "data", "backup"]
        or [item.get("action") for item in items] != ["preserve"] * 4
    ):
        raise error_type(
            "В deploy NAS запрещены mkfs, очистка и создание ZFS. "
            "Для продолжения должны существовать три полностью проверенные ФС"
        )
    serials = [item.get("serial") for item in items]
    if (
        any(not isinstance(serial, str) or not _SERIAL.fullmatch(serial)
            for serial in serials)
        or len(set(serials)) != 4
    ):
        raise error_type("Нельзя однозначно подтвердить четыре серийных номера")
    recovery = plan.get("recovery_fingerprint")
    if recovery is not None and (
        not isinstance(recovery, str) or not _SHA.fullmatch(recovery)
        or recovery != fingerprint
    ):
        raise error_type("Журнал восстановления не соответствует текущим HDD")
    if recovery is not None:
        print("Завершение подтверждённого журнала без форматирования и очистки HDD")
    else:
        print("Подтверждено сохранение ext4/ZFS; форматирование HDD запрещено")

    return forward(
        ["deploy", name, "--_physical_stage", "prepare"],
        input_text=json.dumps({"fingerprint": fingerprint}) + "\n",
    )
