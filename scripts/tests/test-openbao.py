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
            "install_recovery_host_support",
            side_effect=lambda node, repo: calls.append(
                ("install-recovery", f"{node}:{repo}")
            ),
        ),
        patch.object(
            openbao_module,
            "prepare_recovery_git",
            side_effect=lambda node: calls.append(("prepare-recovery", node)),
        ),
        patch.object(
            openbao_module,
            "initialize_openbao_on_host",
            side_effect=lambda node: calls.append(("initialize", node)),
        ),
        patch.object(
            openbao_module,
            "sync_openbao_otp_contract",
            side_effect=lambda node, sources: calls.append(("sync-otp", node)),
        ),
        patch.object(
            openbao_module,
            "check_recovery_contour",
            side_effect=lambda node: calls.append(("check-recovery", node)),
        ),
        patch.object(
            openbao_module,
            "cleanup_transition_state",
            side_effect=lambda node: calls.append(("cleanup-transition", node)),
        ),
    ):
        if openbao_module.run_initialize_openbao(ROOT) != 0:
            fail("Инициализация OpenBao должна завершаться кодом 0")

    if calls != [
        ("install", f"pve:{ROOT}"),
        ("install-recovery", f"pve:{ROOT}"),
        ("prepare-recovery", "pve"),
        ("initialize", "pve"),
        ("sync-otp", "pve"),
        ("check-recovery", "pve"),
        ("cleanup-transition", "pve"),
        ("check-recovery", "pve"),
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
            patch.object(host, "configure_kv_access") as configure_kv,
            patch.object(host, "materialize_runtime_secrets") as materialize,
            patch.object(host, "publish_client_ca") as publish_ca,
            patch.object(host, "publish_host_ca") as publish_host_ca,
            patch.object(host, "ensure_client_signing_role") as ensure_client_role,
            patch.object(host, "ensure_host_signing_role") as ensure_host_role,
            contextlib.redirect_stdout(output),
        ):
            host.initialize()

    if written != [secret_key]:
        fail("Хостовый сценарий не сохранил ожидаемый unseal-ключ")
    ensure_cas.assert_called_once_with(root_token)
    configure_access.assert_called_once_with(root_token)
    configure_kv.assert_called_once_with(root_token)
    materialize.assert_called_once_with()
    publish_ca.assert_called_once_with(root_token)
    publish_host_ca.assert_called_once_with()
    ensure_client_role.assert_called_once_with()
    ensure_host_role.assert_called_once_with()
    if revoked != [root_token]:
        fail("Initial root token не был передан на self-revoke через stdin")
    text = output.getvalue()
    if secret_key in text or root_token in text:
        fail("Хостовый сценарий вывел секрет OpenBao в журнал")


def test_existing_openbao_skips_root_when_ready() -> None:
    host = load_host_module()
    with tempfile.TemporaryDirectory() as tmp:
        key_path = Path(tmp) / "unseal.key"
        access_path = Path(tmp) / "ssh-access.json"
        kv_access_path = Path(tmp) / "kv-access.json"
        key_path.write_text("existing-key\n", encoding="utf-8")
        access_path.write_text("{}\n", encoding="utf-8")
        kv_access_path.write_text("{}\n", encoding="utf-8")
        with (
            patch.object(host, "KEY_PATH", key_path),
            patch.object(host, "SSH_ACCESS_PATH", access_path),
            patch.object(host, "KV_ACCESS_PATH", kv_access_path),
            patch.object(
                host,
                "read_status",
                return_value={"initialized": True, "sealed": False},
            ),
            patch.object(host, "unseal") as unseal,
            patch.object(host, "ssh_cas_ready", return_value=True),
            patch.object(host, "ssh_access_credentials_complete", return_value=True),
            patch.object(host, "client_ca_published", return_value=True),
            patch.object(host, "check_ssh_access") as check_access,
            patch.object(host, "check_kv_access") as check_kv,
            patch.object(host, "materialize_runtime_secrets") as materialize,
            patch.object(host, "publish_host_ca") as publish_host_ca,
            patch.object(host, "ensure_client_signing_role") as ensure_client_role,
            patch.object(host, "ensure_host_signing_role") as ensure_host_role,
            patch.object(host, "generate_temporary_root_token") as generate_root,
        ):
            host.initialize()

    unseal.assert_called_once_with()
    check_access.assert_called_once_with()
    check_kv.assert_called_once_with()
    materialize.assert_called_once_with()
    publish_host_ca.assert_called_once_with()
    ensure_client_role.assert_called_once_with()
    ensure_host_role.assert_called_once_with()
    generate_root.assert_not_called()


def test_existing_openbao_bootstraps_missing_ssh_security() -> None:
    host = load_host_module()
    with tempfile.TemporaryDirectory() as tmp:
        key_path = Path(tmp) / "unseal.key"
        access_path = Path(tmp) / "ssh-access.json"
        kv_access_path = Path(tmp) / "kv-access.json"
        key_path.write_text("existing-key\n", encoding="utf-8")
        with (
            patch.object(host, "KEY_PATH", key_path),
            patch.object(host, "SSH_ACCESS_PATH", access_path),
            patch.object(host, "KV_ACCESS_PATH", kv_access_path),
            patch.object(
                host,
                "read_status",
                return_value={"initialized": True, "sealed": False},
            ),
            patch.object(host, "unseal") as unseal,
            patch.object(host, "ssh_cas_ready", return_value=False),
            patch.object(host, "client_ca_published", return_value=False),
            patch.object(
                host,
                "generate_temporary_root_token",
                return_value="TEMP-ROOT-TOKEN",
            ) as generate_root,
            patch.object(host, "ensure_ssh_cas") as ensure_cas,
            patch.object(host, "configure_ssh_access") as configure_access,
            patch.object(host, "configure_kv_access") as configure_kv,
            patch.object(host, "materialize_runtime_secrets") as materialize,
            patch.object(host, "publish_client_ca") as publish_ca,
            patch.object(host, "publish_host_ca") as publish_host_ca,
            patch.object(host, "ensure_client_signing_role") as ensure_client_role,
            patch.object(host, "ensure_host_signing_role") as ensure_host_role,
            patch.object(host, "revoke_temporary_root_token") as revoke_root,
        ):
            host.initialize()

    unseal.assert_called_once_with()
    generate_root.assert_called_once_with()
    ensure_cas.assert_called_once_with("TEMP-ROOT-TOKEN")
    configure_access.assert_called_once_with("TEMP-ROOT-TOKEN")
    configure_kv.assert_called_once_with("TEMP-ROOT-TOKEN")
    materialize.assert_called_once_with()
    publish_ca.assert_called_once_with("TEMP-ROOT-TOKEN")
    publish_host_ca.assert_called_once_with()
    ensure_client_role.assert_called_once_with()
    ensure_host_role.assert_called_once_with()
    revoke_root.assert_called_once_with("TEMP-ROOT-TOKEN")


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


def test_raw_root_generation_uses_unseal_key_via_stdin() -> None:
    host = load_host_module()
    with tempfile.TemporaryDirectory() as tmp:
        key = Path(tmp) / "unseal.key"
        key.write_text("UNSEAL-KEY-TEST\n", encoding="utf-8")

        calls: list[tuple[tuple[str, ...], str | None]] = []

        def fake_pct_exec(
            *args: str,
            capture: bool = False,
            check: bool = True,
            input_text: str | None = None,
        ):
            del capture, check
            calls.append((tuple(args), input_text))
            return SimpleNamespace(
                returncode=0,
                stdout="TEMP-ROOT-TOKEN\n",
                stderr="",
            )

        with (
            patch.object(host, "KEY_PATH", key),
            patch.object(host, "pct_exec", side_effect=fake_pct_exec),
        ):
            token = host._generate_root_from_running_server()

    if token != "TEMP-ROOT-TOKEN":
        fail("Временный корневой токен искажён")
    if calls != [
        (
            ("python3", "-c", host.GENERATE_ROOT_CODE),
            "UNSEAL-KEY-TEST",
        )
    ]:
        fail(f"Ключ разблокировки передан небезопасно: {calls!r}")
    if "UNSEAL-KEY-TEST" in " ".join(calls[0][0]):
        fail("Ключ разблокировки попал в argv")


def test_temporary_root_window_restores_protected_container() -> None:
    host = load_host_module()
    calls: list[tuple[tuple[str, ...], bool]] = []

    def fake_pct_exec(
        *args: str,
        capture: bool = False,
        check: bool = True,
        input_text: str | None = None,
    ):
        del capture, input_text
        calls.append((tuple(args), check))
        if args[:5] == (
            "docker",
            "inspect",
            "-f",
            "{{.Config.Image}}",
            "openbao",
        ):
            return SimpleNamespace(
                returncode=0,
                stdout="ghcr.io/openbao/openbao:2.7.0\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with (
        patch.object(host, "pct_exec", side_effect=fake_pct_exec),
        patch.object(host, "unseal") as unseal,
        patch.object(
            host,
            "_generate_root_from_running_server",
            return_value="TEMP-ROOT-TOKEN",
        ) as generate,
    ):
        token = host.generate_temporary_root_token()

    if token != "TEMP-ROOT-TOKEN":
        fail("Временный root-токен потерян при возврате защищённого OpenBao")
    generate.assert_called_once_with()
    if unseal.call_count != 2:
        fail("OpenBao должен разблокироваться во временном и штатном режимах")

    argv = [args for args, _ in calls]
    required = [
        ("docker", "stop", "openbao"),
        (
            "docker",
            "run",
            "-d",
            "--name",
            host.TEMP_OPENBAO_CONTAINER,
            "--network",
            "host",
            "--user",
            "0",
            "-e",
            "BAO_SKIP_DROP_ROOT=1",
            "-e",
            "SKIP_CHOWN=1",
            "-v",
            f"{host.CT_OPENBAO_DATA_PATH}:/openbao/file",
            "-v",
            "/etc/infra-manager/openbao/tls:/openbao/tls:ro",
            "-v",
            f"{host.CT_COMPAT_CONFIG_PATH}:/openbao/config/openbao.hcl:ro",
            "ghcr.io/openbao/openbao:2.7.0",
            "server",
        ),
        ("docker", "start", "openbao"),
    ]
    for expected in required:
        if expected not in argv:
            fail(f"Не выполнен обязательный шаг временного OpenBao: {expected!r}")

    if ("rm", "-f", str(host.CT_COMPAT_CONFIG_PATH)) not in argv:
        fail("Временная конфигурация generate-root не удаляется")


def test_client_ca_publication_uses_only_public_key() -> None:
    host = load_host_module()
    key = "ssh-rsa AAAATESTCLIENTCA"

    calls: list[tuple[tuple[str, ...], str | None]] = []

    def fake_pct_exec(
        *args: str,
        capture: bool = False,
        check: bool = True,
        input_text: str | None = None,
    ):
        del capture, check
        calls.append((tuple(args), input_text))
        if args[:2] == ("python3", "-c") and args[2] == host.READ_CLIENT_CA_CODE:
            return SimpleNamespace(returncode=0, stdout=key + "\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with (
        patch.object(host, "pct_exec", side_effect=fake_pct_exec),
        patch.object(host, "client_ca_published", return_value=True),
    ):
        host.publish_client_ca("ROOT-TOKEN-TEST")

    if calls[0][1] != "ROOT-TOKEN-TEST":
        fail("Root token для чтения CA должен передаваться только через stdin")
    if key not in [item[1] for item in calls]:
        fail("Открытый SSH CA ключ не передан в 910")
    if any("ROOT-TOKEN-TEST" in " ".join(item[0]) for item in calls):
        fail("Root token попал в argv при публикации открытого CA")


def test_host_ca_publication_uses_public_endpoint() -> None:
    host = load_host_module()
    key = "ssh-rsa AAAATESTHOSTCA"

    calls: list[tuple[tuple[str, ...], str | None]] = []

    def fake_pct_exec(
        *args: str,
        capture: bool = False,
        check: bool = True,
        input_text: str | None = None,
    ):
        del capture, check
        calls.append((tuple(args), input_text))
        if args[:2] == ("python3", "-c") and args[2] == host.READ_HOST_CA_CODE:
            return SimpleNamespace(returncode=0, stdout=key + "\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with (
        patch.object(host, "pct_exec", side_effect=fake_pct_exec),
        patch.object(host, "host_ca_published", return_value=True),
    ):
        host.publish_host_ca()

    if calls[0][1] is not None:
        fail("Открытый host CA не должен требовать root token")
    if key not in [item[1] for item in calls]:
        fail("Открытый host CA ключ не передан в 910")


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


def test_openbao_container_keeps_protected_files_root_only() -> None:
    compose = (
        ROOT
        / "infrastructure"
        / "guests"
        / "910-infra-manager"
        / "rootfs"
        / "opt"
        / "infra-manager"
        / "compose"
        / "docker-compose.yml"
    ).read_text(encoding="utf-8")
    for item in (
        'BAO_SKIP_DROP_ROOT: "1"',
        'SKIP_CHOWN: "1"',
        '/etc/infra-manager/openbao/tls:/openbao/tls:ro',
    ):
        if item not in compose:
            fail(f"OpenBao Compose не защищает root-only данные: {item}")


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


def test_otp_contract_is_narrow_and_derived() -> None:
    host = load_host_module()
    access_code = host.CONFIGURE_SSH_ACCESS_CODE
    for item in (
        "infra-manager-ssh-otp-config",
        '"/v1/sys/mounts/ssh-otp"',
        '"/v1/sys/auth/machine"',
        '"ssh-otp/roles/guest-*"',
        '"auth/machine/role/guest-*"',
    ):
        if item not in access_code:
            fail(f"Служебный OTP-контур не содержит ограничение: {item}")

    if '"sys/mounts/ssh-otp": {' in access_code:
        fail("ssh-otp-config не должен управлять самим secrets engine")
    if '"sys/auth/machine": {' in access_code:
        fail("ssh-otp-config не должен управлять самим auth method")
    otp_policy = access_code.split("otp_config_policy = json.dumps(", 1)[1].split(
        "request(", 1
    )[0]
    if "sys/policies" in otp_policy:
        fail("ssh-otp-config не должен иметь права изменения ACL-политик")
    if "machine-ssh-otp" not in access_code or "metadata.role_name" not in access_code:
        fail("Машинный OTP должен использовать одну шаблонную ACL-политику")

    if 'request("LIST", "/v1/sys/policies/acl")' not in access_code:
        fail("Список ACL-политик OpenBao должен читаться методом LIST")
    if 'request("GET", "/v1/sys/policies/acl")' in access_code:
        fail("GET для списка ACL-политик OpenBao запрещён")

    code = host.CONFIGURE_OTP_CONTRACT_CODE
    required = (
        'f"guest-{item[\'vmid\']}"',
        '"key_type": "otp"',
        '"default_user": "root"',
        '"allowed_users": "root"',
        '"cidr_list": ",".join(item["target_cidrs"])',
        '"bind_secret_id": True',
        '"secret_id_bound_cidrs": [item["source_cidr"]]',
        '"token_bound_cidrs": [item["source_cidr"]]',
        '"token_ttl": "5m"',
        '"token_max_ttl": "10m"',
        '"token_policies": ["machine-ssh-otp"]',
        '"DELETE"',
    )
    for item in required:
        if item not in code:
            fail(f"OTP/AppRole-контракт не содержит ограничение: {item}")
    if "0.0.0.0/0" in code:
        fail("OTP-контракт не должен разрешать произвольную сеть")


def test_machine_signing_roles_are_separate_and_short_lived() -> None:
    host = load_host_module()
    access_code = host.CONFIGURE_SSH_ACCESS_CODE
    if '"ssh-client-signer/sign/machine-*"' not in access_code:
        fail("PVE-only signer не имеет ограниченного доступа к machine-*")
    if '"ssh-client-signer/roles/*"' not in access_code:
        fail("PVE-only config role не может синхронизировать machine-роли")

    code = host.CONFIGURE_MACHINE_SIGNING_ROLES_CODE
    required = (
        'role_name = f"machine-{vmid}"',
        'principal = f"guest-{vmid}"',
        '"allow_user_certificates": True',
        '"allow_host_certificates": False',
        '"allowed_users": principal',
        '"default_user": principal',
        '"allowed_user_key_lengths": {"ed25519": 0}',
        '"ttl": "2h"',
        'role_name.startswith("machine-")',
        '"DELETE"',
    )
    for item in required:
        if item not in code:
            fail(f"Машинная SSH-роль не содержит ограничение: {item}")

    sign_code = host.SIGN_MACHINE_KEY_CODE
    required_sign = (
        'f"/v1/ssh-client-signer/sign/machine-{vmid}"',
        'principal = f"guest-{vmid}"',
        '"valid_principals": principal',
        '"ttl": "2h"',
        '"/v1/auth/token/revoke-self"',
    )
    for item in required_sign:
        if item not in sign_code:
            fail(f"Машинная SSH-подпись нарушает контракт: {item}")


def test_machine_signing_uses_only_pve_signer_approle() -> None:
    host = load_host_module()
    credentials = {
        "ssh-ca-config": {"role_id": "role-a", "secret_id": "secret-a"},
        "ssh-signer": {"role_id": "role-b", "secret_id": "secret-b"},
        "ssh-otp-config": {"role_id": "role-c", "secret_id": "secret-c"},
    }
    calls: list[tuple[tuple[str, ...], str | None]] = []

    def fake_pct_exec(
        *args: str,
        capture: bool = False,
        check: bool = True,
        input_text: str | None = None,
    ):
        del capture, check
        calls.append((tuple(args), input_text))
        if args[:2] == ("python3", "-c") and args[2] == host.SIGN_MACHINE_KEY_CODE:
            return SimpleNamespace(
                returncode=0,
                stdout="ssh-ed25519-cert-v01@openssh.com AAAAMACHINE\n",
                stderr="",
            )
        return SimpleNamespace(
            returncode=0,
            stdout='{"roles":["machine-410","machine-910"]}\n',
            stderr="",
        )

    with tempfile.TemporaryDirectory() as tmp:
        access = Path(tmp) / "ssh-access.json"
        access.write_text(json.dumps(credentials), encoding="utf-8")
        with (
            patch.object(host, "SSH_ACCESS_PATH", access),
            patch.object(host, "pct_exec", side_effect=fake_pct_exec),
        ):
            host.sync_machine_signing_roles([910, 410])
            certificate = host.sign_machine_public_key(
                410,
                "ssh-ed25519 AAAAPUBLIC machine",
            )

    if not certificate.startswith("ssh-ed25519-cert-v01@openssh.com "):
        fail("Подписанный машинный SSH-сертификат потерян")

    config_payload = json.loads(calls[0][1] or "{}")
    if config_payload.get("credentials") != credentials["ssh-ca-config"]:
        fail("Синхронизация machine-ролей использует неверный AppRole")
    if config_payload.get("vmids") != [410, 910]:
        fail("Синхронизация machine-ролей исказила VMID")

    sign_payload = json.loads(calls[1][1] or "{}")
    if sign_payload.get("credentials") != credentials["ssh-signer"]:
        fail("Машинная подпись использует неверный AppRole")
    if sign_payload.get("vmid") != 410:
        fail("Машинная подпись исказила VMID")


def test_host_signing_role_is_host_only() -> None:
    host = load_host_module()
    code = host.CONFIGURE_HOST_SIGNING_ROLE_CODE
    required = (
        '"algorithm_signer": "rsa-sha2-256"',
        '"allow_user_certificates": False',
        '"allow_host_certificates": True',
        '"allow_bare_domains": True',
        '"allow_subdomains": True',
        '"allowed_domains": "*"',
        '"key_id_format": "infra-manager-host-{{public_key_hash}}"',
        '"ttl": "720h"',
        'role.get("algorithm_signer") != "rsa-sha2-256"',
        'role.get("ttl") != 2592000',
    )
    for item in required:
        if item not in code:
            fail(f"Роль подписи SSH-сервера не содержит ограничение: {item}")
    if "/v1/ssh-host-signer/roles/managed-host" not in code:
        fail("Не настроена отдельная роль managed-host")


def test_host_signing_target_is_verified_on_pve() -> None:
    host = load_host_module()

    def fake_run(argv: list[str], **kwargs: object):
        del kwargs
        if argv[:2] == ["pct", "config"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "hostname: infra-manager\n"
                    "net0: name=eth0,bridge=vmbr0,ip=192.168.9.10/24\n"
                ),
                stderr="",
            )
        raise AssertionError(f"Неожиданная команда: {argv!r}")

    with patch.object(host, "run", side_effect=fake_run):
        principals = host.validate_managed_host_target(
            910,
            "infra-manager",
            "192.168.9.10",
        )
    if principals != ["infra-manager", "192.168.9.10"]:
        fail("PVE-проверка исказила principals host-сертификата")

    with patch.object(host, "run", side_effect=fake_run):
        try:
            host.validate_managed_host_target(
                910,
                "infra-manager",
                "192.168.9.11",
            )
        except host.OpenBaoHostError:
            pass
        else:
            fail("PVE-проверка разрешила чужой IP для VMID 910")

    with patch.object(host, "run", side_effect=fake_run):
        try:
            host.validate_managed_host_target(
                910,
                "infra",
                "192.168.9.10",
            )
        except host.OpenBaoHostError:
            pass
        else:
            fail("PVE-проверка приняла префикс hostname LXC за полное имя")

    def fake_qemu_run(argv: list[str], **kwargs: object):
        del kwargs
        if argv[:2] == ["pct", "config"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        if argv[:2] == ["qm", "config"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "name: infra-manager\n"
                    "ipconfig0: ip=192.168.9.10/24,gw=192.168.9.1\n"
                ),
                stderr="",
            )
        raise AssertionError(f"Неожиданная команда: {argv!r}")

    with patch.object(host, "run", side_effect=fake_qemu_run):
        principals = host.validate_managed_host_target(
            910,
            "infra-manager",
            "192.168.9.10",
        )
    if principals != ["infra-manager", "192.168.9.10"]:
        fail("PVE-проверка исказила principals host-сертификата VM")

    with patch.object(host, "run", side_effect=fake_qemu_run):
        try:
            host.validate_managed_host_target(
                910,
                "infra",
                "192.168.9.10",
            )
        except host.OpenBaoHostError:
            pass
        else:
            fail("PVE-проверка приняла префикс name VM за полное имя")


def test_host_signing_uses_only_signer_approle() -> None:
    host = load_host_module()
    credentials = {
        "ssh-ca-config": {"role_id": "role-a", "secret_id": "secret-a"},
        "ssh-signer": {"role_id": "role-b", "secret_id": "secret-b"},
        "ssh-otp-config": {"role_id": "role-c", "secret_id": "secret-c"},
    }
    calls: list[tuple[tuple[str, ...], str | None]] = []

    def fake_pct_exec(
        *args: str,
        capture: bool = False,
        check: bool = True,
        input_text: str | None = None,
    ):
        del capture, check
        calls.append((tuple(args), input_text))
        return SimpleNamespace(
            returncode=0,
            stdout="ssh-ed25519-cert-v01@openssh.com AAAAHOST\n",
            stderr="",
        )

    with tempfile.TemporaryDirectory() as tmp:
        access = Path(tmp) / "ssh-access.json"
        access.write_text(json.dumps(credentials), encoding="utf-8")
        with (
            patch.object(host, "SSH_ACCESS_PATH", access),
            patch.object(host, "pct_exec", side_effect=fake_pct_exec),
        ):
            certificate = host.sign_host_public_key(
                "ssh-ed25519 AAAAPUBLIC host",
                ["test-host", "192.0.2.10"],
            )

    if not certificate.startswith("ssh-ed25519-cert-v01@openssh.com "):
        fail("Подписанный host-сертификат потерян")
    payload = json.loads(calls[0][1] or "{}")
    if payload.get("credentials") != credentials["ssh-signer"]:
        fail("Host-подпись использует неверный AppRole")
    if payload.get("principals") != ["test-host", "192.0.2.10"]:
        fail("Host-подпись исказила principals")


def test_client_signing_role_is_restricted_to_root() -> None:
    host = load_host_module()
    code = host.CONFIGURE_CLIENT_SIGNING_ROLE_CODE
    required = (
        '"allow_user_certificates": True',
        '"allow_host_certificates": False',
        '"allowed_users": "root"',
        '"default_user": "root"',
        '"allowed_user_key_lengths": {"ed25519": 0}',
        '"key_id_format": "infra-manager-ansible-{{public_key_hash}}"',
        '"default_extensions": {',
        '"permit-pty": ""',
        '"ttl": "15m"',
        '"algorithm_signer": "rsa-sha2-256"',
    )
    for item in required:
        if item not in code:
            fail(f"Роль клиентской подписи не содержит ограничение: {item}")
    if "/v1/ssh-client-signer/roles/infra-manager" not in code:
        fail("Не настроена отдельная роль SSH-подписи infra-manager")


def test_client_signing_keeps_approle_credentials_on_pve() -> None:
    host = load_host_module()
    credentials = {
        "ssh-ca-config": {"role_id": "role-a", "secret_id": "secret-a"},
        "ssh-signer": {"role_id": "role-b", "secret_id": "secret-b"},
        "ssh-otp-config": {"role_id": "role-c", "secret_id": "secret-c"},
    }
    calls: list[tuple[tuple[str, ...], str | None]] = []

    def fake_pct_exec(
        *args: str,
        capture: bool = False,
        check: bool = True,
        input_text: str | None = None,
    ):
        del capture, check
        calls.append((tuple(args), input_text))
        if args[:2] == ("python3", "-c") and args[2] == host.SIGN_CLIENT_KEY_CODE:
            return SimpleNamespace(
                returncode=0,
                stdout="ssh-ed25519-cert-v01@openssh.com AAAATEST\n",
                stderr="",
            )
        return SimpleNamespace(
            returncode=0,
            stdout='{"ready":true}\n',
            stderr="",
        )

    with tempfile.TemporaryDirectory() as tmp:
        access = Path(tmp) / "ssh-access.json"
        access.write_text(json.dumps(credentials), encoding="utf-8")
        with (
            patch.object(host, "SSH_ACCESS_PATH", access),
            patch.object(host, "pct_exec", side_effect=fake_pct_exec),
        ):
            host.ensure_client_signing_role()
            certificate = host.sign_client_public_key(
                "ssh-ed25519 AAAAPUBLIC temp"
            )

    if not certificate.startswith("ssh-ed25519-cert-v01@openssh.com "):
        fail("Подписанный SSH-сертификат потерян")
    for argv, input_text in calls:
        command = " ".join(argv)
        if "secret-a" in command or "secret-b" in command:
            fail("SecretID попал в argv при работе с OpenBao")
        if input_text is None:
            fail("AppRole данные должны передаваться в LXC только через stdin")


def test_ssh_access_credentials_are_pve_only() -> None:
    host = load_host_module()
    credentials = {
        "ssh-ca-config": {"role_id": "role-a", "secret_id": "secret-a"},
        "ssh-signer": {"role_id": "role-b", "secret_id": "secret-b"},
        "ssh-otp-config": {"role_id": "role-c", "secret_id": "secret-c"},
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


def test_kv_contract_is_narrow_and_versioned() -> None:
    host = load_host_module()
    configure = host.CONFIGURE_KV_CODE
    check = host.CHECK_KV_ACCESS_CODE
    materialize = host.MATERIALIZE_RUNTIME_SECRETS_CODE

    required = (
        '"type": "kv"',
        '"options": {"version": "2"}',
        '"pve/api/infra-manager"',
        '"git/github/proxmox-read"',
        '"services/semaphore"',
        '"infra-manager-kv-read"',
        '"kv-reader", "infra-manager-kv-read"',
        '"kv-semaphore-writer", "infra-manager-kv-semaphore-write"',
        '"token_bound_cidrs": ["127.0.0.1/32"]',
    )
    for item in required:
        if item not in configure:
            fail(f"KV v2 не содержит обязательный контракт: {item}")

    if f'{host.KV_MOUNT}/data/*' in configure:
        fail("KV policy не должна выдавать wildcard-доступ ко всем секретам")
    if 'services/forbidden' not in check:
        fail("Проверка KV должна подтверждать запрет постороннего пути")
    for filename in (
        "pve-api.env",
        "github_proxmox_repo_ed25519",
        "semaphore-server.env",
        "initial-admin-password",
        "semaphore-api-token",
    ):
        if filename not in materialize:
            fail(f"Материализация KV не создаёт {filename}")
    if "/run/infra-manager/secrets" not in materialize:
        fail("Рабочие секреты должны материализоваться только в /run")
    for item in (
        "RUNTIME_UID = 1001",
        "RUNTIME_GID = 0",
        "os.fchown(fd, RUNTIME_UID, RUNTIME_GID)",
        "os.chmod(TARGET.parent, 0o750)",
        "os.chmod(TARGET, 0o750)",
        "os.chmod(TARGET / name, 0o600)",
    ):
        if item not in materialize:
            fail(f"Права runtime-секретов нарушают контракт: {item}")


def test_kv_access_credentials_are_pve_only() -> None:
    host = load_host_module()
    credentials = {
        "kv-reader": {"role_id": "role-kv", "secret_id": "secret-kv"},
        "kv-semaphore-writer": {
            "role_id": "role-writer",
            "secret_id": "secret-writer",
        },
    }
    with tempfile.TemporaryDirectory() as tmp:
        key_dir = Path(tmp)
        target = key_dir / "kv-access.json"
        with (
            patch.object(host, "KEY_DIR", key_dir),
            patch.object(host, "KV_ACCESS_PATH", target),
        ):
            host.write_kv_access_credentials(credentials)
        if json.loads(target.read_text(encoding="utf-8")) != credentials:
            fail("Служебные данные KV записаны с искажением")
        if target.stat().st_mode & 0o777 != 0o600:
            fail("Служебные данные KV должны иметь права 0600")


def test_semaphore_token_update_is_narrow() -> None:
    host = load_host_module()
    code = host.UPDATE_SEMAPHORE_API_TOKEN_CODE
    required = (
        'payload.get("credentials")',
        'payload.get("api_token")',
        'data/services/semaphore',
        'if current.get("api_token") != api_token:',
        'updated["api_token"] = api_token',
        '"/v1/auth/token/revoke-self"',
    )
    for item in required:
        if item not in code:
            fail(f"Обновление Semaphore token нарушает контракт: {item}")
    if "data/pve/api/infra-manager" in code:
        fail("Writer Semaphore не должен изменять PVE API credential")
    if "data/git/github/proxmox-read" in code:
        fail("Writer Semaphore не должен изменять Git credential")


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
    test_existing_openbao_skips_root_when_ready()
    test_existing_openbao_bootstraps_missing_ssh_security()
    test_ssh_ca_reconcile_requires_initial_admin_token()
    test_raw_root_generation_uses_unseal_key_via_stdin()
    test_temporary_root_window_restores_protected_container()
    test_client_ca_publication_uses_only_public_key()
    test_host_ca_publication_uses_public_endpoint()
    test_ssh_ca_mounts_are_separate()
    test_openbao_container_keeps_protected_files_root_only()
    test_ssh_access_contract_is_narrow()
    test_otp_contract_is_narrow_and_derived()
    test_machine_signing_roles_are_separate_and_short_lived()
    test_machine_signing_uses_only_pve_signer_approle()
    test_host_signing_role_is_host_only()
    test_host_signing_target_is_verified_on_pve()
    test_host_signing_uses_only_signer_approle()
    test_client_signing_role_is_restricted_to_root()
    test_client_signing_keeps_approle_credentials_on_pve()
    test_ssh_access_credentials_are_pve_only()
    test_kv_contract_is_narrow_and_versioned()
    test_kv_access_credentials_are_pve_only()
    test_semaphore_token_update_is_narrow()
    test_stale_key_is_not_overwritten()
    print("[ОК] Проверки OpenBao пройдены")


if __name__ == "__main__":
    main()
