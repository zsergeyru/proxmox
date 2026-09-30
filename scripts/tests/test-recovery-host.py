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
    module.ACCESS_GITHUB_KEY = (
        module.ACCESS_DIR / "github" / "github_proxmox_repo_ed25519"
    )

    module.OPENBAO_DIR = module.PVE_ONLY_DIR / "openbao"
    module.OPENBAO_UNSEAL_KEY = module.OPENBAO_DIR / "unseal.key"
    module.OPENBAO_SSH_ACCESS = module.OPENBAO_DIR / "ssh-access.json"
    module.OPENBAO_KV_ACCESS = module.OPENBAO_DIR / "kv-access.json"

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
    module.ACCESS_DIR.mkdir(parents=True, exist_ok=True)
    write(module.RECOVERY_GITHUB_KEY, "git-key\n")
    write(module.ACCESS_GITHUB_KEY, "git-key\n")
    write(module.OPENBAO_UNSEAL_KEY)
    write(module.OPENBAO_SSH_ACCESS, "{}\n")
    write(module.OPENBAO_KV_ACCESS, "{}\n")
    write(module.OPENBAO_RAFT_DIR / "raft.db")
    write(module.OPENTOFU_STATE, "{}\n")
    write(module.SEMAPHORE_DB)


def test_prepare_git_recovery() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        configure_paths(module, Path(tmp))
        write(module.ACCESS_GITHUB_KEY, "git-key\n")

        with patch.object(module.os, "chown", lambda *_args: None):
            module.prepare_git_recovery()

        expected = b"git-key\n"
        assert module.RECOVERY_GITHUB_KEY.read_bytes() == expected
        assert module.BOOTSTRAP_GITHUB_KEY.read_bytes() == expected
        assert module.ACCESS_GITHUB_KEY.read_bytes() == expected
        assert module.RECOVERY_GITHUB_KEY.stat().st_mode & 0o777 == 0o600


def test_prepare_rejects_divergent_keys() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        configure_paths(module, Path(tmp))
        write(module.RECOVERY_GITHUB_KEY, "recovery-key\n")
        write(module.ACCESS_GITHUB_KEY, "different-key\n")

        try:
            module.prepare_git_recovery()
        except module.RecoveryError as exc:
            assert "разные GitHub Deploy Key" in str(exc)
        else:
            raise AssertionError("Разные Git key должны блокировать prepare")


def test_restore_git_access_from_recovery() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        configure_paths(module, Path(tmp))
        write(module.RECOVERY_GITHUB_KEY, "git-key\n")

        with patch.object(module.os, "chown", lambda *_args: None):
            module.restore_git_access()

        assert module.BOOTSTRAP_GITHUB_KEY.read_text() == "git-key\n"
        assert module.ACCESS_GITHUB_KEY.read_text() == "git-key\n"


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


def test_preflight_allows_missing_access_directory() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        configure_paths(module, Path(tmp))
        prepare_complete_state(module)

        import shutil

        shutil.rmtree(module.ACCESS_DIR)
        with patch.object(module, "managed_guests_exist", return_value=False):
            module.verify_recovery_state(require_approle=False)

            try:
                module.verify_recovery_state(require_approle=True)
            except module.RecoveryError as exc:
                assert "access" in str(exc)
            else:
                raise AssertionError(
                    "Полный recovery-check должен требовать восстановленный access"
                )


def test_preflight_allows_missing_approle_files() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        configure_paths(module, Path(tmp))
        prepare_complete_state(module)
        module.OPENBAO_SSH_ACCESS.unlink()
        module.OPENBAO_KV_ACCESS.unlink()

        with patch.object(module, "managed_guests_exist", return_value=False):
            module.verify_recovery_state(require_approle=False)

            try:
                module.verify_recovery_state(require_approle=True)
            except module.RecoveryError as exc:
                assert "ssh-access.json" in str(exc) or "kv-access.json" in str(exc)
            else:
                raise AssertionError(
                    "Полный recovery-check должен требовать восстановленные AppRole-файлы"
                )


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


def main() -> None:
    tests = (
        test_prepare_git_recovery,
        test_prepare_rejects_divergent_keys,
        test_restore_git_access_from_recovery,
        test_verify_recovery_state,
        test_preflight_allows_missing_access_directory,
        test_preflight_allows_missing_approle_files,
        test_opentofu_state_required_only_for_managed_guests,
    )
    for test in tests:
        test()
    print(f"[ОК] Проверки recovery helper пройдены: {len(tests)}")


if __name__ == "__main__":
    main()
