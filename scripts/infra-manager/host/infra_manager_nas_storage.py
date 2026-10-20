"""Явное подтверждение плана новых HDD в том же deploy на терминале PVE."""

from __future__ import annotations

import json
import re
import sys


PREFIX = "NAS_STORAGE_PLAN_JSON="


def confirm_nas_storage(name: str, forward, error_type: type[Exception]) -> int:
    """Показать план, запросить подтверждение и передать только его отпечаток."""

    result = forward(["storage-plan", name], capture_output=True)
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
    if not plan.get("requires_creation"):
        return 0
    if [item.get("action") for item in items if isinstance(item, dict)] != [
        "preserve", "create-ext4", "create-zfs", "create-zfs"
    ]:
        raise error_type(
            "Обнаружены ранее настроенные HDD или неполный состав. "
            "Автоматическое форматирование смешанного набора запрещено"
        )
    if not sys.stdin.isatty():
        raise error_type(
            "Для подтверждения форматирования HDD требуется интерактивный терминал PVE"
        )
    print(
        "\nВНИМАНИЕ: подготовка УНИЧТОЖИТ любые нераспознанные данные на "
        "перечисленных трёх HDD. Системный SSD не изменяется."
    )
    answer = input("Подготовить указанные HDD с удалением данных? [да/нет]: ").strip().casefold()
    if answer != "да":
        raise error_type("Подготовка отменена: требуется явное «да», диски не изменены")
    payload = json.dumps({"fingerprint": fingerprint}) + "\n"
    return forward(["storage-prepare", name], input_text=payload)
