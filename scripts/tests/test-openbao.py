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
            patch.object(host, "ensure_ssh_cas") as ensure_cas,
            patch.object(host, "configure_ssh_access") as configure_access,
            contextlib.redirect_stdout(output),
        ):
            host.initialize()

    if written != [secret_key]:
        fail("Хостовый сценарий не сохранил ожидаемый unseal-ключ")
    ensure_cas.assert_called_once_with(root_token)
    configure_access.assert_called_once_with(root_token)
    if revoked != [root_token]:
        fail("Initial root token не был передан на self-revoke через stdin")
    text = output.getvalue()
    if secret_key in text or root_token in text:
        fail("Хостовый сценарий вывел секрет OpenBao в журнал")


def test_existing_openbao_ensures_ssh_cas() -> None:
    host = load_host_module()
    with tempfile.TemporaryDirectory() as tmp:
        key_path = Path(tmp) / "unseal.key"
        key_path.write_text("existing-key\n", encoding="utf-8")
        with (
            patch.object(host, "KEY_PATH", key_path),
            patch.object(
                host,
                "read_status",
                return_value={"initialized": True, "sealed": False},
            ),
            patch.object(host, "unseal") as unseal,
            patch.object(host, "ensure_ssh_cas") as ensure_cas,
            patch.object(host, "check_ssh_access") as check_access,
        ):
            host.initialize()

    unseal.assert_called_once_with()
    ensure_cas.assert_called_once_with()
    check_access.assert_called_once_with()


def test_ssh_ca_reconcile_requires_initial_admin_token() -> None:
    host = load_host_module()

    with (
        patch.object(host, "ssh_cas_ready", return_value=True),
        patch.object(host, "configure_ssh_cas") as configure,
    ):
        host.ensure_ssh_cas()
    configure.assert_not_called()

    with (
        patch.object(host, "ssh_cas_ready", return_value=False),
        patch.object(host, "configure_ssh_cas") as configure,
    ):
        try:
            host.ensure_ssh_cas()
        except host.OpenBaoHostError as exc:
            if "явная миграция" not in str(exc):
                fail(f"Неожиданная ошибка старого OpenBao: {exc}")
        else:
            fail("Старый OpenBao без SSH CA принят без административного доступа")
    configure.assert_not_called()


def test_ssh_ca_mounts_are_separate() -> None:
    host = load_host_module()
    code = host.CONFIGURE_SSH_CA_CODE
    if '"ssh-client-signer"' not in code:
        fail("Не зафиксирован отдельный центр подписи клиентов")
    if '"ssh-host-signer"' not in code:
        fail("Не зафиксирован отдельный центр подписи серверов")
    if code.count('"generate_signing_key": True') != 1:
        fail("SSH CA должны создаваться единым идемпотентным механизмом")
    if "client == host" not in code:
        fail("Два SSH-центра должны проверяться как независимые")


def test_ssh_access_contract_is_narrow() -> None:
    host = load_host_module()
    code = host.CONFIGURE_SSH_ACCESS_CODE
    if "infra-manager-ssh-ca-config" not in code:
        fail("Не создана отдельная политика настройки SSH CA")
    if "infra-manager-ssh-signer" not in code:
        fail("Не создана отдельная политика подписи SSH")
    if 'path "ssh-client-signer/config/ca"' in code:
        fail("Служебная политика не должна давать доступ к закрытому CA")
    if 'path "sys/policies' in code:
        fail("Служебная политика не должна позволять менять политики")
    if '"token_bound_cidrs": ["127.0.0.1/32"]' not in code:
        fail("Служебные токены OpenBao должны работать только локально")


def test_ssh_access_credentials_are_pve_only() -> None:
    host = load_host_module()
    credentials = {
        "ssh-ca-config": {"role_id": "role-a", "secret_id": "secret-a"},
        "ssh-signer": {"role_id": "role-b", "secret_id": "secret-b"},
    }
    with tempfile.TemporaryDirectory() as tmp:
        key_dir = Path(tmp)
        target = key_dir / "ssh-access.json"
        with (
            patch.object(host, "KEY_DIR", key_dir),
            patch.object(host, "SSH_ACCESS_PATH", target),
        ):
            host.write_ssh_access_credentials(credentials)
        if json.loads(target.read_text(encoding="utf-8")) != credentials:
            fail("Служебные данные OpenBao записаны с искажением")
        if target.stat().st_mode & 0o777 != 0o600:
            fail("Служебные данные OpenBao должны иметь права 0600")


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
    test_existing_openbao_ensures_ssh_cas()
    test_ssh_ca_reconcile_requires_initial_admin_token()
    test_ssh_ca_mounts_are_separate()
    test_ssh_access_contract_is_narrow()
    test_ssh_access_credentials_are_pve_only()
    test_stale_key_is_not_overwritten()
    print("[ОК] Проверки OpenBao пройдены")


if __name__ == "__main__":
    main()
