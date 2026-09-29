#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = ROOT / "scripts" / "infra-manager"
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager import openbao as openbao_module


def fail(message: str) -> None:
    raise SystemExit(message)


def load_host_module():
    path = ROOT / "scripts/infra-manager/host/openbao-unseal.py"
    spec = importlib.util.spec_from_file_location("openbao_host_test", path)
    if spec is None or spec.loader is None:
        fail("Не удалось загрузить хостовый сценарий OpenBao")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_orchestration() -> None:
    calls: list[tuple[str, str]] = []
    with (
        patch.dict(
            os.environ,
            {"TF_VAR_pve_endpoint": "https://pve:8006"},
            clear=False,
        ),
        patch.object(
            openbao_module,
            "install_openbao_host_support",
            side_effect=lambda node, repo: calls.append(
                ("install", f"{node}:{repo}")
            ),
        ),
        patch.object(
            openbao_module,
            "initialize_openbao_on_host",
            side_effect=lambda node: calls.append(("initialize", node)),
        ),
    ):
        if openbao_module.run_initialize_openbao(ROOT) != 0:
            fail("Инициализация OpenBao должна завершаться кодом 0")

    if calls != [
        ("install", f"pve:{ROOT}"),
        ("initialize", "pve"),
    ]:
        fail(f"Неожиданный порядок инициализации OpenBao: {calls!r}")


def test_host_initialization_does_not_print_secrets() -> None:
    host = load_host_module()
    secret_key = "UNSEAL-SECRET-TEST"
    root_token = "ROOT-TOKEN-SECRET-TEST"
    init_payload = {
        "keys_base64": [secret_key],
        "root_token": root_token,
    }
    statuses = iter(
        [
            {"initialized": False, "sealed": True},
            {"initialized": True, "sealed": False},
        ]
    )
    written: list[str] = []
    revoked: list[str] = []

    def fake_pct_exec(
        *args: str,
        capture: bool = False,
        check: bool = True,
        input_text: str | None = None,
    ):
        del capture, check
        if args[:2] == ("python3", "-c") and args[2] == host.INIT_CODE:
            if input_text is not None:
                fail("Инициализация OpenBao не должна получать секрет через stdin")
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(init_payload),
                stderr="",
            )
        if args[:2] == ("python3", "-c") and args[2] == host.REVOKE_ROOT_CODE:
            revoked.append(input_text or "")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        fail(f"Неожиданный pct exec при инициализации: {args!r}")
        raise AssertionError

    output = io.StringIO()
    with tempfile.TemporaryDirectory() as tmp:
        test_key = Path(tmp) / "unseal.key"
        with (
            patch.object(host, "KEY_PATH", test_key),
            patch.object(host, "read_status", side_effect=lambda wait: next(statuses)),
            patch.object(host, "pct_exec", side_effect=fake_pct_exec),
            patch.object(host, "write_unseal_key", side_effect=written.append),
            patch.object(host, "unseal"),
            contextlib.redirect_stdout(output),
        ):
            host.initialize()

    if written != [secret_key]:
        fail("Хостовый сценарий не сохранил ожидаемый unseal-ключ")
    if revoked != [root_token]:
        fail("Initial root token не был передан на self-revoke через stdin")
    text = output.getvalue()
    if secret_key in text or root_token in text:
        fail("Хостовый сценарий вывел секрет OpenBao в журнал")


def test_stale_key_is_not_overwritten() -> None:
    host = load_host_module()
    with tempfile.TemporaryDirectory() as tmp:
        stale = Path(tmp) / "unseal.key"
        stale.write_text("stale\n", encoding="utf-8")
        with (
            patch.object(host, "KEY_PATH", stale),
            patch.object(
                host,
                "read_status",
                return_value={"initialized": False, "sealed": True},
            ),
        ):
            try:
                host.initialize()
            except host.OpenBaoHostError:
                pass
            else:
                fail("Старый unseal-ключ был бы перезаписан автоматически")


def main() -> None:
    test_orchestration()
    test_host_initialization_does_not_print_secrets()
    test_stale_key_is_not_overwritten()
    print("[ОК] Проверки OpenBao пройдены")


if __name__ == "__main__":
    main()
