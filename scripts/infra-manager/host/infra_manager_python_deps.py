"""Проверенная установка недостающей библиотеки Python в управляющем госте.

Используется только штатным deploy подтверждённого LXC с физического PVE.
Работа с хранилищами, повторная выдача SSH-доверия и пересоздание LXC запрещены.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def restore_manager_python_dependency(
    vmid: int, *, run: Callable[..., Any], error_type: type[Exception],
) -> None:
    """Восстановить разрешённый пакет APT до запуска установленного оператора."""

    check = ["pct", "exec", str(vmid), "--", "python3", "-c", "import jsonschema"]
    result = run(check, check=False, capture=True)
    if result.returncode == 0:
        return
    if "ModuleNotFoundError: No module named 'jsonschema'" not in (result.stderr or ""):
        raise error_type(
            "Системный Python управляющего гостя завершился неизвестной ошибкой; "
            "автоматическое изменение пакетов запрещено"
        )
    print("[ИНФО] Установка недостающего python3-jsonschema через штатный APT")
    run(["pct", "exec", str(vmid), "--", "apt-get", "update"])
    run([
        "pct", "exec", str(vmid), "--", "env",
        "DEBIAN_FRONTEND=noninteractive", "apt-get", "install",
        "-y", "python3-jsonschema",
    ])
    if run(check, check=False, capture=True).returncode != 0:
        raise error_type("Не удалось восстановить python3-jsonschema в управляющем госте")
