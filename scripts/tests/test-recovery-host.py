#!/usr/bin/env python3
"""Проверки PVE-only recovery helper."""

from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts/infra-manager/host/recovery.py"


def load_module():
    spec = importlib.util.spec_from_file_location("recovery_host", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("Не удалось загрузить recovery.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def configure_paths(module, root: Path) -> None:
    module.PERSISTENT_ROOT = root / "infra-manager"
    module.PVE_ONLY_DIR = module.PERSISTENT_ROOT / "pve-only"
    module.ACCESS_DIR = module.PERSISTENT_ROOT / "access"
    module.STATE_DIR = module.PERSISTENT_ROOT / "state"

    module.RECOVERY_DIR = module.PVE_ONLY_DIR / "recovery"
    module.RECOVERY_GITHUB_KEY = (
        module.RECOVERY_DIR / "github_proxmox_repo_ed25519"
    )
    module.BOOTSTRAP_DIR = root / "bootstrap"
    module.BOOTSTRAP_GITHUB_KEY = (
        module.BOOTSTRAP_DIR / "github_proxmox_repo_ed25519"
    )

    module.OPENBAO_DIR = module.PVE_ONLY_DIR / "openbao"
    module.OPENBAO_UNSEAL_KEY = module.OPENBAO_DIR / "unseal.key"
    module.OPENBAO_SSH_ACCESS = module.OPENBAO_DIR / "ssh-access.json"
    module.OPENBAO_KV_ACCESS = module.OPENBAO_DIR / "kv-access.json"
    module.OPENBAO_OPERATOR_ACCESS = (
        module.OPENBAO_DIR / "operator-access.json"
    )
    module.OPENBAO_TLS_DIR = module.PVE_ONLY_DIR / "openbao-tls"
    module.OPENBAO_TLS_CA_KEY = module.OPENBAO_TLS_DIR / "ca.key"
    module.OPENBAO_TLS_CA_CERT = module.OPENBAO_TLS_DIR / "ca.crt"

    module.OPENBAO_RAFT_DIR = module.STATE_DIR / "openbao" / "raft"
    module.OPENTOFU_STATE = (
        module.STATE_DIR / "opentofu" / "state" / "proxmox.tfstate"
    )
    module.SEMAPHORE_DB = (
        module.STATE_DIR / "semaphore" / "semaphore.sqlite"
    )
    module.LOCK_PATH = root / "recovery.lock"


def write(path: Path, value: str = "test\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def prepare_complete_state(module) -> None:
    write(module.RECOVERY_GITHUB_KEY, "git-key\n")
    write(module.OPENBAO_UNSEAL_KEY)
    write(module.OPENBAO_SSH_ACCESS, "{}\n")
    write(module.OPENBAO_KV_ACCESS, "{}\n")
    write(
        module.OPENBAO_OPERATOR_ACCESS,
        '{"username":"operator","password":"recovery-test-password"}\n',
    )
    write(module.OPENBAO_RAFT_DIR / "raft.db")
    write(module.OPENTOFU_STATE, "{}\n")
    write(module.SEMAPHORE_DB)


def test_prepare_git_recovery() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        configure_paths(module, Path(tmp))
        write(module.BOOTSTRAP_GITHUB_KEY, "git-key\n")

        with patch.object(module.os, "chown", lambda *_args: None):
            module.prepare_git_recovery()

        expected = b"git-key\n"
        assert module.RECOVERY_GITHUB_KEY.read_bytes() == expected
        assert module.BOOTSTRAP_GITHUB_KEY.read_bytes() == expected
        assert module.RECOVERY_GITHUB_KEY.stat().st_mode & 0o777 == 0o600
        assert module.RECOVERY_DIR.stat().st_mode & 0o777 == 0o700
        assert module.BOOTSTRAP_DIR.stat().st_mode & 0o777 == 0o700


def test_restore_git_access_from_recovery() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        configure_paths(module, Path(tmp))
        write(module.RECOVERY_GITHUB_KEY, "git-key\n")

        with patch.object(module.os, "chown", lambda *_args: None):
            module.restore_git_access()

        assert module.BOOTSTRAP_GITHUB_KEY.read_text() == "git-key\n"
        assert module.BOOTSTRAP_DIR.stat().st_mode & 0o777 == 0o700


def test_verify_recovery_state() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        configure_paths(module, Path(tmp))
        prepare_complete_state(module)
        with patch.object(module, "managed_guests_exist", return_value=False):
            module.verify_recovery_state()

            module.OPENBAO_UNSEAL_KEY.unlink()
            try:
                module.verify_recovery_state()
            except module.RecoveryError as exc:
                assert "unseal.key" in str(exc)
            else:
                raise AssertionError(
                    "Отсутствующий unseal key должен блокировать recovery"
                )


def test_full_check_does_not_require_access_directory() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        configure_paths(module, Path(tmp))
        prepare_complete_state(module)

        with patch.object(module, "managed_guests_exist", return_value=False):
            module.verify_recovery_state(require_approle=False)
            module.verify_recovery_state(require_approle=True)


def test_preflight_allows_missing_approle_files() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        configure_paths(module, Path(tmp))
        prepare_complete_state(module)
        module.OPENBAO_SSH_ACCESS.unlink()
        module.OPENBAO_KV_ACCESS.unlink()
        module.OPENBAO_OPERATOR_ACCESS.unlink()

        with patch.object(module, "managed_guests_exist", return_value=False):
            module.verify_recovery_state(require_approle=False)

            try:
                module.verify_recovery_state(require_approle=True)
            except module.RecoveryError as exc:
                assert any(
                    name in str(exc)
                    for name in (
                        "ssh-access.json",
                        "kv-access.json",
                        "operator-access.json",
                    )
                )
            else:
                raise AssertionError(
                    "Полный recovery-check должен требовать восстановленные AppRole-файлы"
                )


def test_tls_ca_required_after_otp_migration() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        configure_paths(module, Path(tmp))
        prepare_complete_state(module)
        write(
            module.OPENBAO_SSH_ACCESS,
            '{"ssh-ca-config":{},"ssh-signer":{},"ssh-otp-config":{}}\n',
        )

        with patch.object(module, "managed_guests_exist", return_value=False):
            try:
                module.verify_recovery_state()
            except module.RecoveryError as exc:
                assert "openbao-tls" in str(exc)
            else:
                raise AssertionError(
                    "После OTP-миграции потеря TLS CA должна блокировать recovery"
                )

            write(module.OPENBAO_TLS_CA_KEY)
            write(module.OPENBAO_TLS_CA_CERT)
            module.verify_recovery_state()


def test_opentofu_state_required_only_for_managed_guests() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        configure_paths(module, Path(tmp))
        prepare_complete_state(module)
        module.OPENTOFU_STATE.unlink()

        with patch.object(module, "managed_guests_exist", return_value=False):
            module.verify_recovery_state()

        with patch.object(module, "managed_guests_exist", return_value=True):
            try:
                module.verify_recovery_state()
            except module.RecoveryError as exc:
                assert "OpenTofu state" in str(exc)
            else:
                raise AssertionError(
                    "Managed-гости без OpenTofu state должны блокировать recovery"
                )


def test_run_recovery_owns_bootstrap() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        configure_paths(module, Path(tmp))
        prepare_complete_state(module)

        commands: list[tuple[list[str], dict[str, object]]] = []

        def fake_download(target: Path) -> None:
            target.write_text("#!/usr/bin/env bash\n", encoding="utf-8")

        def fake_run(argv: list[str], **kwargs: object):
            commands.append((argv, kwargs))
            return type("Result", (), {"returncode": 0})()

        with (
            patch.object(module, "managed_guests_exist", return_value=False),
            patch.object(module.os, "chown", lambda *_args: None),
            patch.object(module, "_download_bootstrap", side_effect=fake_download),
            patch.object(module.subprocess, "run", side_effect=fake_run),
        ):
            module.run_recovery()

        assert module.BOOTSTRAP_GITHUB_KEY.read_text() == "git-key\n"
        assert len(commands) == 1
        argv, options = commands[0]
        assert argv[0] == "bash"
        assert len(argv) == 2, "Публичная команда не должна получать параметр --recover"
        assert options["env"]["PROXMOX_BOOTSTRAP_INTERNAL_RECOVERY"] == "1"


def main() -> None:
    tests = (
        test_prepare_git_recovery,
        test_restore_git_access_from_recovery,
        test_verify_recovery_state,
        test_full_check_does_not_require_access_directory,
        test_preflight_allows_missing_approle_files,
        test_tls_ca_required_after_otp_migration,
        test_opentofu_state_required_only_for_managed_guests,
        test_run_recovery_owns_bootstrap,
    )
    for test in tests:
        test()
    print(f"[ОК] Проверки recovery helper пройдены: {len(tests)}")


if __name__ == "__main__":
    main()
