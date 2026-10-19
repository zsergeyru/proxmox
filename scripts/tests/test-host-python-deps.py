#!/usr/bin/env python3
"""Проверить загрузку оператора 910 без внешней библиотеки проверки схем."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_host_dependency() -> None:
    """Статус не требует jsonschema, а проверка дисков требует её явно."""

    manifest = yaml.safe_load(
        (ROOT / "infrastructure/guests/910-infra-manager/provision.yaml")
        .read_text(encoding="utf-8")
    )
    assert "python3-jsonschema" in manifest["system"]["required_packages"]

    program = """
import builtins
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import_orig = builtins.__import__

def deny_jsonschema(name, *args, **kwargs):
    if name == "jsonschema" or name.startswith("jsonschema."):
        raise ModuleNotFoundError("No module named 'jsonschema'")
    return import_orig(name, *args, **kwargs)

builtins.__import__ = deny_jsonschema
from infra_manager.operator import build_parser
assert build_parser().parse_args(["status", "910"]).command == "status"
from infra_manager.storage_catalog import validate_storage
from infra_manager.common import InfraManagerError
try:
    validate_storage(Path(sys.argv[2]), "nas", {})
except InfraManagerError as exc:
    assert "python3-jsonschema" in str(exc), str(exc)
else:
    raise AssertionError("Непроверенная схема дисков была принята")
print("[ОК] Оператор доступен без jsonschema, проверка дисков отказывает")
"""

    result = subprocess.run(
        [sys.executable, "-c", program,
         str(ROOT / "scripts/infra-manager"), str(ROOT)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


if __name__ == "__main__":
    test_host_dependency()
    print("[ОК] Зависимости системного Python управляющего гостя проверены")
