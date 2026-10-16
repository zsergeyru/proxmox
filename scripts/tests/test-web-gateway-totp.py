#!/usr/bin/env python3
"""Проверка первичной привязки Google Authenticator без настоящих секретов."""

from __future__ import annotations

import importlib.util
import io
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "automation/ansible/roles/web_gateway/files/initialize-totp.py"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def load():
    spec = importlib.util.spec_from_file_location("web_gateway_totp", SCRIPT)
    require(spec is not None and spec.loader is not None, "Нет сценария TOTP")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test() -> None:
    mod = load()
    with TemporaryDirectory() as dirname:
        path = Path(dirname) / "admin.png"

        def generated(argv, **kwargs):
            require(argv[0:3] == ["docker", "exec", "web-gateway-authelia"],
                    "Генерация должна происходить только внутри Authelia")
            require("generate" in argv and "admin" in argv and "--force" not in argv,
                    "Нельзя сбрасывать зарегистрированный TOTP")
            require(argv[-2:] == ["--path", "/data/totp-setup/admin.png"],
                    "QR должен попасть в закрытый каталог")
            require(kwargs.get("stdout") is not None and kwargs.get("stderr") is not None,
                    "Секреты нельзя выводить через стандартные потоки")
            path.write_bytes(b"imitation-png")
            return SimpleNamespace(returncode=0, stdout=b"otpauth://secret", stderr=b"")

        with patch.object(mod.subprocess, "run", side_effect=generated) as command:
            with redirect_stdout(io.StringIO()) as output:
                result = mod.initialize(
                    "web-gateway-authelia", "admin", "zsergey.ru", path,
                    "/data/totp-setup/admin.png",
                )
            require(command.call_count == 1, "При первой установке CLI должна вызываться")
            require(path.stat().st_mode & 0o777 == 0o600,
                    "QR должен быть доступен только владельцу")
            require("secret" not in result and "secret" not in output.getvalue(),
                    "Секрет TOTP не должен попадать в журнал")
            mod.initialize(
                "web-gateway-authelia", "admin", "zsergey.ru", path,
                "/data/totp-setup/admin.png",
            )
            require(command.call_count == 1,
                    "Повторное развёртывание не должно менять ключ второго фактора")

        path.unlink()
        with patch.object(mod.subprocess, "run", return_value=SimpleNamespace(
            returncode=1, stdout=b"", stderr=b"Configuration already exists",
        )) as command:
            result = mod.initialize(
                "web-gateway-authelia", "admin", "zsergey.ru", path,
                "/data/totp-setup/admin.png",
            )
            require("уже есть" in result and command.call_count == 1,
                    "Существующий TOTP нельзя сбрасывать ради нового QR")
            require(not path.exists(), "Отсутствующий QR не надо создавать вручную")

        with patch.object(mod.subprocess, "run", return_value=SimpleNamespace(
            returncode=1, stdout=b"", stderr=b"Storage failure",
        )):
            try:
                mod.initialize(
                    "web-gateway-authelia", "admin", "zsergey.ru", path,
                    "/data/totp-setup/admin.png",
                )
            except RuntimeError:
                pass
            else:
                raise AssertionError("Ошибка записи SQLite должна остановить развёртывание")

    print("140: TOTP готовится штатной CLI, ключ не меняется при повторном развёртывании")


if __name__ == "__main__":
    test()
