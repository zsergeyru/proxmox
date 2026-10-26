"""Явное подтверждение плана новых HDD в том же deploy на терминале PVE."""

from __future__ import annotations

import json
import re
import sys


PREFIX = "NAS_STORAGE_PLAN_JSON="


def confirm_nas_storage(name: str, forward, error_type: type[Exception]) -> int:
    """Показать план, запросить подтверждение и передать только его отпечаток."""

    result = forward(["deploy", name, "--_physical_stage", "plan"], capture_output=True)
    if result.stdout:
        for line in result.stdout.splitlines():
            if not line.startswith(PREFIX):
                print(line)
    if result.returncode:
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        return result.returncode

    matches = [line.removeprefix(PREFIX) for line in result.stdout.splitlines()
               if line.startswith(PREFIX)]
    if len(matches) != 1:
        raise error_type("Infra Manager не вернул однозначный план новых HDD")
    try:
        plan = json.loads(matches[0])
    except (json.JSONDecodeError, TypeError) as exc:
        raise error_type("Не удалось проверить план подготовки HDD") from exc
    fingerprint = plan.get("fingerprint") if isinstance(plan, dict) else None
    items = plan.get("items") if isinstance(plan, dict) else None
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise error_type("Контрольный отпечаток дисков имеет неверный формат")
    if not isinstance(items, list) or len(items) != 4:
        raise error_type("План HDD не содержит все четыре диска")
    if not all(isinstance(item, dict) for item in items):
        raise error_type("План HDD содержит некорректное описание диска")
    if [item.get("role") for item in items] != ["system", "shared", "data", "backup"]:
        raise error_type("План HDD содержит неполное или неоднозначное назначение ролей")
    actions = [item.get("action") for item in items]
    recovery_fingerprint = plan.get("recovery_fingerprint")
    if recovery_fingerprint is not None:
        if (
            not isinstance(recovery_fingerprint, str)
            or not re.fullmatch(r"[0-9a-f]{64}", recovery_fingerprint)
            or plan.get("requires_creation")
            or actions != ["preserve"] * 4
        ):
            raise error_type(
                "Журнал восстановления NAS не соответствует текущим дискам. "
                "Никакое форматирование не разрешено"
            )
        print(
            "Незавершённая ранее подтверждённая подготовка NAS: "
            "проверка существующих UUID/GUID и регистрация OMV "
            "БЕЗ повторного форматирования"
        )
        payload = json.dumps({"fingerprint": recovery_fingerprint}) + "\n"
        return forward(["deploy", name, "--_physical_stage", "prepare"], input_text=payload)
    if not plan.get("requires_creation"):
        return 0
    if actions not in (
        ["preserve", "create-ext4", "create-zfs", "create-zfs"],
        ["preserve", "preserve", "create-zfs", "create-zfs"],
    ):
        raise error_type(
            "План HDD не поддерживается автоматической подготовкой. "
            "Разрешено создать два новых ZFS-диска, сохранив ранее настроенный ext4, "
            "либо подготовить три пустых HDD"
        )
    to_create = items[1:] if actions[1] == "create-ext4" else items[2:]
    serials = [item.get("serial") for item in items]
    if any(
        not isinstance(serial, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", serial)
        for serial in serials
    ) or len(set(serials)) != 4:
        raise error_type("Не удалось однозначно определить серийные номера накопителей")
    if not sys.stdin.isatty():
        raise error_type(
            "Для подтверждения форматирования HDD требуется интерактивный терминал PVE"
        )
    print("\nВНИМАНИЕ: будут подготовлены только следующие диски:")
    for item in to_create:
        state = item.get("current_state")
        if (
            not isinstance(state, str)
            or not state.startswith("Устройство сейчас /dev/")
            or len(state) > 900
            or any(char in state for char in "\r\n\x1b")
        ):
            raise error_type(
                "NAS не сообщил безопасные сведения о текущей разметке HDD; "
                "подтверждение форматирования запрещено"
            )
        print(f"  {item['role']}: {item['serial']} ({item['action']})")
        print(f"    {state}")
    if actions[1] == "preserve":
        print(f"Существующий HDD 1 ТБ ({items[1]['serial']}) НЕ ФОРМАТИРУЕТСЯ.")
        print("Перед записью на новые диски NAS проверит его ext4, UUID, OMV и подключение.")
    print("Форматирование уничтожит данные на перечисленных новых дисках.")
    answer = input("Подготовить ТОЛЬКО указанные новые HDD? [да/нет]: ").strip().casefold()
    if answer != "да":
        raise error_type("Подготовка отменена: требуется явное «да», диски не изменены")
    payload = json.dumps({"fingerprint": fingerprint}) + "\n"
    return forward(["deploy", name, "--_physical_stage", "prepare"], input_text=payload)
