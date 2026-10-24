"""Ask for host-declared operator credentials on the trusted PVE terminal."""

from __future__ import annotations

import getpass
import json
import re
import sys


_PREFIX = "PHYSICAL_CREDENTIAL_PLAN_JSON="
_NAME = re.compile(r"^[a-z][a-z0-9-]*$")


def prompt_physical_credentials(name: str, forward, error_type: type[Exception]) -> int:
    """Request only declared secrets and deliver them over operator stdin."""
    response = forward(["credential-plan", name], capture_output=True)
    if response.returncode:
        if response.stderr:
            print(response.stderr, file=sys.stderr)
        return response.returncode
    records = [line[len(_PREFIX):] for line in response.stdout.splitlines()
               if line.startswith(_PREFIX)]
    if len(records) != 1:
        raise error_type("Контроллер не предоставил однозначный план секретов")
    try:
        plan = json.loads(records[0])
    except (ValueError, TypeError) as exc:
        raise error_type("Некорректная декларация запросов учётных данных") from exc
    if not isinstance(plan, dict) or plan.get("host") != name:
        raise error_type("Получена декларация для другого физического сервера")
    fields = plan.get("credentials")
    if not isinstance(fields, list) or len(fields) > 12:
        raise error_type("Некорректный перечень операторских данных")
    if fields and not sys.stdin.isatty():
        raise error_type("Для ввода пароля физического сервера нужен терминал PVE")
    for spec in fields:
        if (not isinstance(spec, dict)
                or set(spec) != {"id", "prompt", "min_length", "max_length"}
                or not isinstance(spec["id"], str) or not _NAME.fullmatch(spec["id"])
                or not isinstance(spec["prompt"], str) or not 4 <= len(spec["prompt"]) <= 150
                or any(ord(c) < 32 or ord(c) == 127 for c in spec["prompt"])
                or not isinstance(spec["min_length"], int)
                or not isinstance(spec["max_length"], int)
                or not 4 <= spec["min_length"] <= spec["max_length"] <= 128):
            raise error_type("Неверные параметры секретного ввода")
        value = getpass.getpass(spec["prompt"])
        if (not spec["min_length"] <= len(value) <= spec["max_length"]
                or any(c in value for c in ("\n", "\r", "\x00"))):
            raise error_type("Введённое значение не соответствует ограничениям")
        payload = json.dumps(
            {"id": spec["id"], "password": value}, ensure_ascii=False,
        ) + "\n"
        del value
        result = forward(["credential-set", name], input_text=payload)
        if result:
            return result
    return 0
