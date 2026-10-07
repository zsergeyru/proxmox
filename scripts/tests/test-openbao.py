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
            "repair_openbao_on_host",
            side_effect=lambda node: calls.append(("ensure", node)),
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
    ):
        if openbao_module.run_initialize_openbao(ROOT) != 0:
            fail("Инициализация OpenBao должна завершаться кодом 0")

    if calls != [
        ("install", f"pve:{ROOT}"),
        ("install-recovery", f"pve:{ROOT}"),
        ("prepare-recovery", "pve"),
        ("ensure", "pve"),
        ("sync-otp", "pve"),
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
            patch.object(
                host,
                "configure_operator_access",
            ) as configure_operator,
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
    configure_operator.assert_called_once_with(root_token)
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


def test_waits_for_active_raft_node() -> None:
    host = load_host_module()
    responses = iter(
        [
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "http_status": 429,
                        "body": {
                            "initialized": True,
                            "sealed": False,
                            "standby": True,
                        },
                    }
                ),
                stderr="",
            ),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "http_status": 200,
                        "body": {
                            "initialized": True,
                            "sealed": False,
                            "standby": False,
                        },
                    }
                ),
                stderr="",
            ),
        ]
    )

    def fake_pct_exec(
        *args: str,
        capture: bool = False,
        check: bool = True,
        input_text: str | None = None,
    ):
        del capture, check, input_text
        if args[:2] != ("python3", "-c") or args[2] != host.HEALTH_CODE:
            fail(f"Неожиданная команда health: {args!r}")
        return next(responses)

    with (
        patch.object(host, "pct_exec", side_effect=fake_pct_exec),
        patch.object(host.time, "sleep") as sleep,
    ):
        body = host.wait_until_active(attempts=3, delay=0.1)

    if body.get("standby") is not False:
        fail("OpenBao должен считаться готовым только после перехода active")
    sleep.assert_called_once_with(0.1)


def test_unseal_waits_for_active_node_when_already_unsealed() -> None:
    host = load_host_module()
    with (
        patch.object(
            host,
            "read_status",
            return_value={"initialized": True, "sealed": False},
        ),
        patch.object(host, "wait_until_active") as wait_active,
    ):
        host.unseal()
    wait_active.assert_called_once_with()


def test_repair_recovers_single_node_raft() -> None:
    host = load_host_module()
    with (
        patch.object(host, "prepare_tls_material") as prepare_tls,
        patch.object(
            host,
            "initialize",
            side_effect=[
                host.OpenBaoRaftInactiveError(
                    "OpenBao разблокирован, но Raft-узел не стал активным"
                ),
                None,
            ],
        ) as initialize,
        patch.object(
            host,
            "recover_single_node_raft",
            return_value=Path("/backup/openbao"),
        ) as recover,
    ):
        host.repair()

    prepare_tls.assert_called_once_with()
    if initialize.call_count != 2:
        fail("Repair должен повторить initialize после восстановления Raft")
    recover.assert_called_once_with()


def test_single_node_raft_recovery_writes_expected_peer() -> None:
    host = load_host_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        state = root / "state" / "openbao"
        storage = state / "raft"
        inner = storage / "raft"
        inner.mkdir(parents=True)
        (storage / "raft.db").write_bytes(b"raft-state")
        key = root / "pve-only" / "openbao" / "unseal.key"
        key.parent.mkdir(parents=True)
        key.write_text("unseal-key\n", encoding="utf-8")
        peers = inner / "peers.json"
        backup = root / "backup"
        backup.mkdir()

        calls: list[tuple[str, ...]] = []

        def fake_pct_exec(
            *args: str,
            capture: bool = False,
            check: bool = True,
            input_text: str | None = None,
        ):
            del capture, check, input_text
            calls.append(tuple(args))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        def consume_peers() -> None:
            if not peers.is_file():
                fail("Recovery должен создать peers.json до unseal")
            payload = json.loads(peers.read_text(encoding="utf-8"))
            if payload != [
                {
                    "id": "infra-manager",
                    "address": "127.0.0.1:8201",
                    "non_voter": False,
                }
            ]:
                fail(f"Некорректный одноузловой peers.json: {payload!r}")
            peers.unlink()

        with (
            patch.object(host, "PVE_OPENBAO_STATE_PATH", state),
            patch.object(host, "PVE_RAFT_STORAGE_PATH", storage),
            patch.object(host, "PVE_RAFT_PEERS_PATH", peers),
            patch.object(host, "KEY_PATH", key),
            patch.object(host, "_verify_single_node_raft_contract"),
            patch.object(host, "_backup_openbao_state", return_value=backup),
            patch.object(host, "pct_exec", side_effect=fake_pct_exec),
            patch.object(host, "unseal", side_effect=consume_peers) as unseal,
            patch.object(host, "wait_until_active") as wait_active,
        ):
            returned = host.recover_single_node_raft()

        if returned != backup:
            fail("Recovery должен вернуть путь резервной копии")
        unseal.assert_called_once_with()
        wait_active.assert_called_once_with()
        required = {
            ("docker", "stop", "openbao"),
            ("docker", "start", "openbao"),
        }
        if not required.issubset(set(calls)):
            fail(f"Recovery не выполнил обязательный stop/start: {calls!r}")


def test_single_node_raft_recovery_rejects_multi_node_config() -> None:
    host = load_host_module()
    with tempfile.TemporaryDirectory() as tmp:
        storage = Path(tmp) / "raft"
        storage.mkdir()
        (storage / "raft.db").write_bytes(b"raft-state")
        guest_config = (
            "hostname: infra-manager\n"
            "description: owner=proxmox-project;role=infra-manager\n"
        )
        openbao_config = """
storage "raft" {
  path    = "/openbao/file/raft"
  node_id = "infra-manager"
  retry_join {
    leader_api_addr = "https://other:8200"
  }
}
cluster_addr = "http://127.0.0.1:8201"
listener "tcp" {
  cluster_address = "127.0.0.1:8201"
}
"""

        def fake_run(
            argv: list[str],
            *,
            capture: bool = False,
            check: bool = True,
            input_text: str | None = None,
        ):
            del capture, check, input_text
            if argv[:2] == ["pct", "config"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout=guest_config,
                    stderr="",
                )
            fail(f"Неожиданная команда: {argv!r}")
            raise AssertionError

        def fake_pct_exec(
            *args: str,
            capture: bool = False,
            check: bool = True,
            input_text: str | None = None,
        ):
            del capture, check, input_text
            if args[:2] == ("cat", str(host.CT_OPENBAO_CONFIG_PATH)):
                return SimpleNamespace(
                    returncode=0,
                    stdout=openbao_config,
                    stderr="",
                )
            fail(f"Неожиданная pct-команда: {args!r}")
            raise AssertionError

        with (
            patch.object(host, "VMID", 910),
            patch.object(host, "TLS_DNS_NAME", "infra-manager"),
            patch.object(host, "PVE_RAFT_STORAGE_PATH", storage),
            patch.object(host, "run", side_effect=fake_run),
            patch.object(host, "pct_exec", side_effect=fake_pct_exec),
        ):
            try:
                host._verify_single_node_raft_contract()
            except host.OpenBaoHostError as exc:
                if "многоузловая" not in str(exc):
                    fail(f"Неожиданный отказ recovery: {exc}")
            else:
                fail("Многоузловая конфигурация не должна допускать auto-recovery")



def test_existing_openbao_skips_root_when_ready() -> None:
    host = load_host_module()
    with tempfile.TemporaryDirectory() as tmp:
        key_path = Path(tmp) / "unseal.key"
        access_path = Path(tmp) / "ssh-access.json"
        kv_access_path = Path(tmp) / "kv-access.json"
        operator_access_path = Path(tmp) / "operator-access.json"
        key_path.write_text("existing-key\n", encoding="utf-8")
        access_path.write_text("{}\n", encoding="utf-8")
        kv_access_path.write_text("{}\n", encoding="utf-8")
        operator_access_path.write_text(
            '{"username":"operator","password":"test-password-012345678901234567890"}\n',
            encoding="utf-8",
        )

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(host, "KEY_PATH", key_path))
            stack.enter_context(patch.object(host, "SSH_ACCESS_PATH", access_path))
            stack.enter_context(patch.object(host, "KV_ACCESS_PATH", kv_access_path))
            stack.enter_context(
                patch.object(host, "OPERATOR_ACCESS_PATH", operator_access_path)
            )
            stack.enter_context(
                patch.object(
                    host,
                    "read_status",
                    return_value={"initialized": True, "sealed": False},
                )
            )
            unseal = stack.enter_context(patch.object(host, "unseal"))
            stack.enter_context(
                patch.object(host, "ssh_cas_ready", return_value=True)
            )
            stack.enter_context(
                patch.object(
                    host,
                    "ssh_access_credentials_complete",
                    return_value=True,
                )
            )
            stack.enter_context(
                patch.object(host, "client_ca_published", return_value=True)
            )
            check_access = stack.enter_context(
                patch.object(host, "check_ssh_access")
            )
            check_kv = stack.enter_context(
                patch.object(host, "check_kv_access")
            )
            check_operator = stack.enter_context(
                patch.object(host, "check_operator_access")
            )
            materialize = stack.enter_context(
                patch.object(host, "materialize_runtime_secrets")
            )
            publish_host_ca = stack.enter_context(
                patch.object(host, "publish_host_ca")
            )
            ensure_client_role = stack.enter_context(
                patch.object(host, "ensure_client_signing_role")
            )
            ensure_host_role = stack.enter_context(
                patch.object(host, "ensure_host_signing_role")
            )
            generate_root = stack.enter_context(
                patch.object(host, "generate_temporary_root_token")
            )
            host.initialize()

    unseal.assert_called_once_with()
    check_access.assert_called_once_with()
    check_kv.assert_called_once_with()
    check_operator.assert_called_once_with()
    materialize.assert_called_once_with()
    publish_host_ca.assert_called_once_with()
    ensure_client_role.assert_called_once_with()
    ensure_host_role.assert_called_once_with()
    generate_root.assert_not_called()


def test_existing_openbao_recovers_broken_kv_approle() -> None:
    host = load_host_module()
    with tempfile.TemporaryDirectory() as tmp:
        key_path = Path(tmp) / "unseal.key"
        access_path = Path(tmp) / "ssh-access.json"
        kv_access_path = Path(tmp) / "kv-access.json"
        operator_access_path = Path(tmp) / "operator-access.json"
        key_path.write_text("existing-key\n", encoding="utf-8")
        access_path.write_text("{}\n", encoding="utf-8")
        kv_access_path.write_text("{}\n", encoding="utf-8")
        operator_access_path.write_text(
            '{"username":"operator","password":"test-password-012345678901234567890"}\n',
            encoding="utf-8",
        )

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(host, "KEY_PATH", key_path))
            stack.enter_context(patch.object(host, "SSH_ACCESS_PATH", access_path))
            stack.enter_context(patch.object(host, "KV_ACCESS_PATH", kv_access_path))
            stack.enter_context(
                patch.object(host, "OPERATOR_ACCESS_PATH", operator_access_path)
            )
            stack.enter_context(
                patch.object(
                    host,
                    "read_status",
                    return_value={"initialized": True, "sealed": False},
                )
            )
            unseal = stack.enter_context(patch.object(host, "unseal"))
            stack.enter_context(
                patch.object(host, "ssh_cas_ready", return_value=True)
            )
            stack.enter_context(
                patch.object(
                    host,
                    "ssh_access_credentials_complete",
                    return_value=True,
                )
            )
            stack.enter_context(
                patch.object(host, "client_ca_published", return_value=True)
            )
            check_ssh = stack.enter_context(
                patch.object(host, "check_ssh_access")
            )
            check_kv = stack.enter_context(
                patch.object(
                    host,
                    "check_kv_access",
                    side_effect=host.OpenBaoHostError(
                        "HTTP 500 from stale kv-reader SecretID"
                    ),
                )
            )
            check_operator = stack.enter_context(
                patch.object(host, "check_operator_access")
            )
            generate_root = stack.enter_context(
                patch.object(
                    host,
                    "generate_temporary_root_token",
                    return_value="TEMP-ROOT-TOKEN",
                )
            )
            ensure_cas = stack.enter_context(
                patch.object(host, "ensure_ssh_cas")
            )
            reconcile_kv = stack.enter_context(
                patch.object(host, "reconcile_kv_access")
            )
            materialize = stack.enter_context(
                patch.object(host, "materialize_runtime_secrets")
            )
            publish_ca = stack.enter_context(
                patch.object(host, "publish_client_ca")
            )
            publish_host_ca = stack.enter_context(
                patch.object(host, "publish_host_ca")
            )
            ensure_client_role = stack.enter_context(
                patch.object(host, "ensure_client_signing_role")
            )
            ensure_host_role = stack.enter_context(
                patch.object(host, "ensure_host_signing_role")
            )
            revoke_root = stack.enter_context(
                patch.object(host, "revoke_temporary_root_token")
            )
            host.initialize()

    unseal.assert_called_once_with()
    if check_kv.call_count != 1:
        fail("Сломанный KV AppRole должен обнаруживаться ровно одной проверкой")
    if check_ssh.call_count != 2:
        fail("SSH AppRole должен повторно подтверждаться после recovery-окна")
    if check_operator.call_count != 2:
        fail(
            "Операторский userpass должен повторно подтверждаться "
            "после recovery-окна"
        )
    generate_root.assert_called_once_with()
    ensure_cas.assert_called_once_with("TEMP-ROOT-TOKEN")
    reconcile_kv.assert_called_once_with("TEMP-ROOT-TOKEN")
    materialize.assert_called_once_with()
    publish_ca.assert_called_once_with("TEMP-ROOT-TOKEN")
    publish_host_ca.assert_called_once_with()
    ensure_client_role.assert_called_once_with()
    ensure_host_role.assert_called_once_with()
    revoke_root.assert_called_once_with("TEMP-ROOT-TOKEN")


def test_existing_openbao_reconciles_operator_policy() -> None:
    host = load_host_module()
    with tempfile.TemporaryDirectory() as tmp:
        key_path = Path(tmp) / "unseal.key"
        access_path = Path(tmp) / "ssh-access.json"
        kv_access_path = Path(tmp) / "kv-access.json"
        operator_access_path = Path(tmp) / "operator-access.json"
        key_path.write_text("existing-key\n", encoding="utf-8")
        access_path.write_text("{}\n", encoding="utf-8")
        kv_access_path.write_text("{}\n", encoding="utf-8")
        operator_access_path.write_text(
            '{"username":"operator","password":"test-password-012345678901234567890"}\n',
            encoding="utf-8",
        )

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(host, "KEY_PATH", key_path))
            stack.enter_context(patch.object(host, "SSH_ACCESS_PATH", access_path))
            stack.enter_context(patch.object(host, "KV_ACCESS_PATH", kv_access_path))
            stack.enter_context(
                patch.object(host, "OPERATOR_ACCESS_PATH", operator_access_path)
            )
            stack.enter_context(
                patch.object(host, "ssh_cas_ready", return_value=True)
            )
            stack.enter_context(
                patch.object(
                    host,
                    "ssh_access_credentials_complete",
                    return_value=True,
                )
            )
            stack.enter_context(
                patch.object(host, "client_ca_published", return_value=True)
            )
            check_ssh = stack.enter_context(
                patch.object(host, "check_ssh_access")
            )
            check_kv = stack.enter_context(
                patch.object(host, "check_kv_access")
            )
            stack.enter_context(
                patch.object(
                    host,
                    "check_operator_access",
                    side_effect=host.OpenBaoHostError(
                        "operator lacks required UI ACL"
                    ),
                )
            )
            generate_root = stack.enter_context(
                patch.object(
                    host,
                    "generate_temporary_root_token",
                    return_value="TEMP-ROOT-TOKEN",
                )
            )
            stack.enter_context(patch.object(host, "ensure_ssh_cas"))
            configure_operator = stack.enter_context(
                patch.object(host, "configure_operator_access")
            )
            stack.enter_context(
                patch.object(host, "materialize_runtime_secrets")
            )
            stack.enter_context(patch.object(host, "publish_client_ca"))
            stack.enter_context(patch.object(host, "publish_host_ca"))
            stack.enter_context(
                patch.object(host, "ensure_client_signing_role")
            )
            stack.enter_context(
                patch.object(host, "ensure_host_signing_role")
            )
            revoke_root = stack.enter_context(
                patch.object(host, "revoke_temporary_root_token")
            )
            host.ensure_existing_openbao_ssh()

    if check_ssh.call_count != 2 or check_kv.call_count != 2:
        fail("Здоровые SSH/KV доступы должны подтверждаться после admin-окна")
    generate_root.assert_called_once_with()
    configure_operator.assert_called_once_with("TEMP-ROOT-TOKEN")
    revoke_root.assert_called_once_with("TEMP-ROOT-TOKEN")


def test_existing_openbao_bootstraps_missing_ssh_security() -> None:
    host = load_host_module()
    with tempfile.TemporaryDirectory() as tmp:
        key_path = Path(tmp) / "unseal.key"
        access_path = Path(tmp) / "ssh-access.json"
        kv_access_path = Path(tmp) / "kv-access.json"
        operator_access_path = Path(tmp) / "operator-access.json"
        key_path.write_text("existing-key\n", encoding="utf-8")

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(host, "KEY_PATH", key_path))
            stack.enter_context(patch.object(host, "SSH_ACCESS_PATH", access_path))
            stack.enter_context(patch.object(host, "KV_ACCESS_PATH", kv_access_path))
            stack.enter_context(
                patch.object(host, "OPERATOR_ACCESS_PATH", operator_access_path)
            )
            stack.enter_context(
                patch.object(
                    host,
                    "read_status",
                    return_value={"initialized": True, "sealed": False},
                )
            )
            unseal = stack.enter_context(patch.object(host, "unseal"))
            stack.enter_context(
                patch.object(host, "ssh_cas_ready", return_value=False)
            )
            stack.enter_context(
                patch.object(host, "client_ca_published", return_value=False)
            )
            generate_root = stack.enter_context(
                patch.object(
                    host,
                    "generate_temporary_root_token",
                    return_value="TEMP-ROOT-TOKEN",
                )
            )
            ensure_cas = stack.enter_context(
                patch.object(host, "ensure_ssh_cas")
            )
            configure_access = stack.enter_context(
                patch.object(host, "configure_ssh_access")
            )
            configure_kv = stack.enter_context(
                patch.object(host, "configure_kv_access")
            )
            configure_operator = stack.enter_context(
                patch.object(host, "configure_operator_access")
            )
            materialize = stack.enter_context(
                patch.object(host, "materialize_runtime_secrets")
            )
            publish_ca = stack.enter_context(
                patch.object(host, "publish_client_ca")
            )
            publish_host_ca = stack.enter_context(
                patch.object(host, "publish_host_ca")
            )
            ensure_client_role = stack.enter_context(
                patch.object(host, "ensure_client_signing_role")
            )
            ensure_host_role = stack.enter_context(
                patch.object(host, "ensure_host_signing_role")
            )
            revoke_root = stack.enter_context(
                patch.object(host, "revoke_temporary_root_token")
            )
            host.initialize()

    unseal.assert_called_once_with()
    generate_root.assert_called_once_with()
    ensure_cas.assert_called_once_with("TEMP-ROOT-TOKEN")
    configure_access.assert_called_once_with(
        "TEMP-ROOT-TOKEN",
        rotate_secret_ids=False,
    )
    configure_kv.assert_called_once_with("TEMP-ROOT-TOKEN")
    configure_operator.assert_called_once_with("TEMP-ROOT-TOKEN")
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

    with patch.object(host, "ssh_cas_ready", return_value=False):
        try:
            host.check_ssh_access()
        except host.OpenBaoHostError:
            pass
        else:
            fail("Проверка SSH-доступов должна отклонять неготовые SSH CA")

    access_code = host.CONFIGURE_SSH_ACCESS_CODE
    for item in (
        "infra-manager-ssh-otp-config",
        '"/v1/sys/mounts/ssh-otp"',
        '"/v1/sys/auth/machine"',
        '"ssh-otp/roles/guest-*"',
        '"auth/machine/role/+"',
    ):
        if item not in access_code:
            fail(f"Служебный OTP-контур не содержит ограничение: {item}")

    if '"sys/mounts/ssh-otp": {' in access_code:
        fail("ssh-otp-config не должен управлять самим secrets engine")
    if '"sys/auth/machine": {' in access_code:
        fail("ssh-otp-config не должен управлять самим auth method")

    check_code = host.CHECK_SSH_ACCESS_CODE
    for item in (
        '"/v1/sys/mounts"',
        '"ssh-otp/"',
        '"/v1/sys/auth"',
        '"machine/"',
        '"approle"',
    ):
        if item not in check_code:
            fail(f"Проверка служебного доступа не подтверждает OTP-контур: {item}")

    otp_policy = access_code.split("otp_config_policy = json.dumps(", 1)[1].split(
        "request(", 1
    )[0]
    if "sys/policies" in otp_policy:
        fail("ssh-otp-config не должен иметь права изменения ACL-политик")
    if "machine-ssh-otp" not in access_code or "metadata.role_name" not in access_code:
        fail("Машинный OTP должен использовать одну шаблонную ACL-политику")

    role_policy = access_code.split('"auth/machine/role/+": {', 1)[1].split(
        '"auth/machine/role/+/bind-secret-id"', 1
    )[0]
    for item in (
        '"required_parameters"',
        '"allowed_parameters"',
        '"role_name": ["guest-*"]',
        '"token_policies": ["machine-ssh-otp"]',
        '"token_ttl": ["5m"]',
        '"token_max_ttl": ["10m"]',
        '"token_no_default_policy": ["true"]',
    ):
        if item not in role_policy:
            fail(f"Параметры машинного AppRole не ограничены: {item}")
    if '"*": []' in role_policy:
        fail("Машинному AppRole запрещены произвольные параметры")

    if '"capabilities": ["create", "read", "update", "delete"]' in role_policy:
        fail("Путь записи машинного AppRole не должен иметь read")
    for suffix in (
        "bind-secret-id",
        "secret-id-bound-cidrs",
        "secret-id-num-uses",
        "secret-id-ttl",
        "token-bound-cidrs",
        "token-num-uses",
        "token-ttl",
        "token-max-ttl",
        "policies",
        "role-id",
    ):
        needle = f'"auth/machine/role/+/{suffix}"'
        if needle not in access_code:
            fail(f"Не разрешена узкая проверка AppRole: {suffix}")

    if '"auth/machine/role/guest-*/role-id"' in access_code:
        fail("Подресурсы AppRole не должны использовать '*' внутри пути")

    for suffix in (
        "bind-secret-id",
        "secret-id-bound-cidrs",
        "secret-id-num-uses",
        "secret-id-ttl",
        "token-bound-cidrs",
        "token-num-uses",
        "token-ttl",
        "token-max-ttl",
        "policies",
        "role-id",
        "secret-id",
    ):
        nested = access_code.split(
            f'"auth/machine/role/+/{suffix}": {{', 1
        )[1].split("            },", 1)[0]
        if '"role_name": ["guest-*"]' not in nested:
            fail(f"Подресурс AppRole не ограничен guest-*: {suffix}")

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
        '"secret_id_bound_cidrs": [item["source_cidr"]]',
        '"token_bound_cidrs": [item["source_cidr"]]',
        '"token_ttl": "5m"',
        '"token_max_ttl": "10m"',
        '"token_policies": "machine-ssh-otp"',
        '"token_no_default_policy": "true"',
        'read_role_value("bind-secret-id", "bind_secret_id") is not True',
        'read_role_value("secret-id-num-uses", "secret_id_num_uses") != 0',
        'read_role_value("secret-id-ttl", "secret_id_ttl") != 0',
        'read_role_value("token-num-uses", "token_num_uses") != 0',
        'read_role_value("token-ttl", "token_ttl") != 300',
        'read_role_value("token-max-ttl", "token_max_ttl") != 600',
        '"DELETE"',
    )
    for item in required:
        if item not in code:
            fail(f"OTP/AppRole-контракт не содержит ограничение: {item}")
    if "0.0.0.0/0" in code:
        fail("OTP-контракт не должен разрешать произвольную сеть")


def test_machine_credentials_are_issued_narrowly() -> None:
    host = load_host_module()
    access_code = host.CONFIGURE_SSH_ACCESS_CODE
    if '"auth/machine/role/+/secret-id"' not in access_code:
        fail("Служебная OTP-роль не может выпустить SecretID гостя")
    if '"auth/machine/role/guest-*/secret-id"' in access_code:
        fail("SecretID subpath не должен использовать '*' внутри пути")
    issue_code = host.ISSUE_MACHINE_CREDENTIALS_CODE
    required = (
        'f"guest-{vmid}"',
        'f"/v1/auth/machine/role/{role_name}/role-id"',
        'f"/v1/auth/machine/role/{role_name}/secret-id"',
        '"POST"',
        '"/v1/auth/token/revoke-self"',
    )
    for item in required:
        if item not in issue_code:
            fail(f"Выдача машинной идентичности нарушает контракт: {item}")
    for forbidden in ("pathlib", "Path(", "write_text", "write_bytes"):
        if forbidden in issue_code:
            fail("Машинные RoleID/SecretID не должны записываться на диск")


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


def test_operator_access_credentials_are_pve_only() -> None:
    host = load_host_module()
    credentials = {
        "username": "operator",
        "password": "operator-password-012345678901234567890",
    }
    with tempfile.TemporaryDirectory() as tmp:
        key_dir = Path(tmp)
        target = key_dir / "operator-access.json"
        with (
            patch.object(host, "KEY_DIR", key_dir),
            patch.object(host, "OPERATOR_ACCESS_PATH", target),
        ):
            host.write_operator_access_credentials(credentials)
            actual = host.read_operator_access_credentials()

        if actual != credentials:
            fail("Учётные данные оператора записаны с искажением")
        if target.stat().st_mode & 0o777 != 0o600:
            fail("Учётные данные оператора должны иметь права 0600")


def test_operator_policy_is_narrow_and_visible_in_ui() -> None:
    host = load_host_module()
    configure = host.CONFIGURE_OPERATOR_ACCESS_CODE
    check = host.CHECK_OPERATOR_ACCESS_CODE

    for expected in (
        "infra-operator",
        "/v1/sys/auth/userpass",
        '"listing_visibility": "unauth"',
        '"token_no_default_policy": True',
        '"token_ttl": "1h"',
        '"token_max_ttl": "8h"',
        '"infra-secrets/data/*"',
        '"sys/auth"',
        '"sys/policies/acl"',
        '"sys/internal/ui/resultant-acl"',
        '"ssh-client-signer/roles"',
        '"ssh-host-signer/roles"',
        '"ssh-otp/roles"',
    ):
        if expected not in configure:
            fail(f"Операторский контракт OpenBao не содержит {expected}")

    if "/v1/sys/internal/ui/mounts" not in check:
        fail("Проверка оператора не подтверждает видимость userpass в UI")
    if '"sys/internal/ui/resultant-acl": {"read"}' not in check:
        fail("Проверка оператора не требует ACL, необходимый встроенному UI")
    for forbidden in (
        '"sys/storage/raft/configuration"',
        '"sys/init"',
    ):
        if forbidden not in check:
            fail(f"Проверка оператора не контролирует запрет {forbidden}")

    policy_prefix = configure.split(
        "operator_policy = json.dumps(",
        1,
    )[1].split(
        'request(\n    "POST",\n    "/v1/sys/policies/acl/infra-operator"',
        1,
    )[0]
    for forbidden in (
        '"sys/storage/raft/configuration": {',
        '"sys/init": {',
        '"sys/auth/*": {',
        '"sys/policies/acl/*": {"capabilities": ["create"',
    ):
        if forbidden in policy_prefix:
            fail(f"Политика оператора получила запрещённый доступ: {forbidden}")

    if '"sys/internal/ui/resultant-acl"' in host.CONFIGURE_SSH_ACCESS_CODE:
        fail(
            "UI-specific resultant-acl не должен попадать "
            "в служебные AppRole policy"
        )


def test_corrupted_operator_file_generates_new_password() -> None:
    host = load_host_module()
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "operator-access.json"
        target.write_text("{broken", encoding="utf-8")
        generated = "generated-operator-password-012345678901234"
        with (
            patch.object(host, "OPERATOR_ACCESS_PATH", target),
            patch.object(
                host.secrets,
                "token_urlsafe",
                return_value=generated,
            ),
            patch.object(
                host,
                "pct_exec",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout='{"ready":true}',
                    stderr="",
                ),
            ) as pct_exec,
            patch.object(host, "check_operator_access") as check_access,
            patch.object(host, "write_operator_access_credentials") as write_access,
        ):
            result = host.configure_operator_access("TEMP-ROOT")

    if result != {"username": "operator", "password": generated}:
        fail("Повреждённый operator-access.json не был восстановлен")
    payload = json.loads(pct_exec.call_args.kwargs["input_text"])
    if payload != {
        "root_token": "TEMP-ROOT",
        "username": "operator",
        "password": generated,
    }:
        fail("Новые операторские данные переданы OpenBao некорректно")
    check_access.assert_called_once_with(result)
    write_access.assert_called_once_with(result)


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
    test_waits_for_active_raft_node()
    test_unseal_waits_for_active_node_when_already_unsealed()
    test_repair_recovers_single_node_raft()
    test_single_node_raft_recovery_writes_expected_peer()
    test_single_node_raft_recovery_rejects_multi_node_config()
    test_existing_openbao_skips_root_when_ready()
    test_existing_openbao_recovers_broken_kv_approle()
    test_existing_openbao_reconciles_operator_policy()
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
    test_machine_credentials_are_issued_narrowly()
    test_host_signing_role_is_host_only()
    test_host_signing_target_is_verified_on_pve()
    test_host_signing_uses_only_signer_approle()
    test_client_signing_role_is_restricted_to_root()
    test_client_signing_keeps_approle_credentials_on_pve()
    test_ssh_access_credentials_are_pve_only()
    test_kv_contract_is_narrow_and_versioned()
    test_kv_access_credentials_are_pve_only()
    test_operator_access_credentials_are_pve_only()
    test_operator_policy_is_narrow_and_visible_in_ui()
    test_corrupted_operator_file_generates_new_password()
    test_semaphore_token_update_is_narrow()
    test_stale_key_is_not_overwritten()
    print("[ОК] Проверки OpenBao пройдены")


if __name__ == "__main__":
    main()
