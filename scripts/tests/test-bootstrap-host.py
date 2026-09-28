#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts/bootstrap-runner/bootstrap-host.py"

spec = importlib.util.spec_from_file_location("bootstrap_host", MODULE_PATH)
if spec is None or spec.loader is None:
    raise SystemExit("Не удалось загрузить bootstrap-host.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

BootstrapError = module.BootstrapError
BootstrapHost = module.BootstrapHost


def assert_equal(actual, expected, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}\nОжидалось: {expected!r}\nПолучено: {actual!r}")


class OwnershipHarness(BootstrapHost):
    def __init__(self, config: str) -> None:
        super().__init__("apply")
        self._config = config

    def infra_exists(self) -> bool:
        return True

    def pct_config(self, ctid: int) -> str:
        if ctid != self.infra_ctid:
            raise AssertionError(f"Неожиданный VMID: {ctid}")
        return self._config


def test_strict_ownership_marker() -> None:
    good = """hostname: infra-manager
unprivileged: 1
description: Постоянный LXC [owner=proxmox-project;role=infra-manager]
"""
    if not OwnershipHarness(good).infra_config_is_expected():
        raise AssertionError("Ожидаемый 910 должен проходить строгую проверку владения")

    variants = (
        good.replace("hostname: infra-manager", "hostname: foreign"),
        good.replace("unprivileged: 1", "unprivileged: 0"),
        good.replace("owner=proxmox-project;", ""),
        good.replace("role=infra-manager", "role=other"),
    )
    for config in variants:
        if OwnershipHarness(config).infra_config_is_expected():
            raise AssertionError(
                "Неполный контракт владения не должен приниматься за infra-manager"
            )


class ApplyHarness(BootstrapHost):
    def __init__(self, mode: str, *, infra_exists: bool, owns_state: bool) -> None:
        super().__init__(mode)
        self._infra_exists = infra_exists
        self._owns_state = owns_state
        self.events: list[str] = []

    def infra_exists(self) -> bool:
        self.events.append("infra_exists")
        return self._infra_exists

    def prepare_runner(self) -> None:
        self.events.append("prepare_runner")

    def runner_owns_910(self) -> bool:
        self.events.append("runner_owns_910")
        return self._owns_state

    def ensure_existing_infra_running(self) -> None:
        self.events.append("ensure_existing")

    def deploy_910_phase(self, phase: str, title: str, success: str) -> None:
        del title, success
        self.events.append(f"deploy:{phase}")

    def ensure_infra_container_features(self) -> None:
        self.events.append("ensure_features")

    def handoff_infra(self, access_mode: str = "apply") -> None:
        self.events.append(f"handoff:{access_mode}")

    def prepare_infra_pve_access(self, access_mode: str = "apply") -> None:
        self.events.append(f"pve_access:{access_mode}")

    def handoff_existing_infra(self) -> None:
        self.events.append("handoff_existing")

    def verify_infra_ready(self) -> None:
        self.events.append("verify_ready")

    def finalize_runner(self) -> None:
        self.events.append("finalize_runner")

    def check_ready(self) -> None:
        self.events.append("check_ready")

    def info(self, message: str) -> None:
        self.events.append(f"info:{message}")


def test_new_install_flow() -> None:
    host = ApplyHarness("apply", infra_exists=False, owns_state=False)
    host.apply()
    assert_equal(
        host.events,
        [
            "infra_exists",
            "prepare_runner",
            "runner_owns_910",
            "deploy:infrastructure",
            "ensure_features",
            "deploy:base",
            "handoff:apply",
            "deploy:provision",
            "verify_ready",
            "finalize_runner",
            "check_ready",
        ],
        "Новая установка должна пройти полный путь создания 910",
    )


def test_existing_without_bootstrap_state() -> None:
    host = ApplyHarness("apply", infra_exists=True, owns_state=False)
    host.apply()
    assert_equal(
        host.events,
        [
            "infra_exists",
            "prepare_runner",
            "runner_owns_910",
            "ensure_existing",
            "handoff_existing",
            "deploy:existing",
            "verify_ready",
            "finalize_runner",
            "check_ready",
        ],
        "Существующий 910 без state 990 должен обновляться без OpenTofu-владения 910",
    )


def test_resume_unfinished_initial_state() -> None:
    host = ApplyHarness("apply", infra_exists=True, owns_state=True)
    host.apply()
    expected_prefix = [
        "infra_exists",
        "prepare_runner",
        "runner_owns_910",
    ]
    assert_equal(
        host.events[:3],
        expected_prefix,
        "Незавершённая установка должна сначала обнаружить state 990",
    )
    if "deploy:infrastructure" not in host.events:
        raise AssertionError("Незавершённая установка должна продолжить OpenTofu state")
    if "ensure_features" not in host.events:
        raise AssertionError(
            "Незавершённая установка должна восстановить host-only LXC features"
        )
    if "deploy:existing" in host.events:
        raise AssertionError(
            "Незавершённая установка не должна переходить на existing-путь"
        )


def test_recover_existing_without_state() -> None:
    host = ApplyHarness("recover", infra_exists=True, owns_state=False)
    host.apply()
    assert_equal(
        host.events,
        [
            "infra_exists",
            "prepare_runner",
            "runner_owns_910",
            "ensure_existing",
            "pve_access:recover",
            "handoff_existing",
            "deploy:existing",
            "verify_ready",
            "finalize_runner",
            "check_ready",
        ],
        "Recovery существующего 910 должен сначала восстановить PVE-доступ",
    )


def test_recover_unfinished_initial_state() -> None:
    host = ApplyHarness("recover", infra_exists=True, owns_state=True)
    host.apply()
    if "handoff:recover" not in host.events:
        raise AssertionError(
            "Recovery незавершённой первоначальной установки должен передать режим recover"
        )
    if "pve_access:recover" in host.events:
        raise AssertionError(
            "Незавершённый первоначальный state должен использовать общий handoff recover"
        )


class ExecuteHarness(BootstrapHost):
    def __init__(self, mode: str) -> None:
        super().__init__(mode)
        self.events: list[str] = []

    def require_host(self) -> None:
        self.events.append("require_host")

    def verify_runner_contract(self) -> None:
        self.events.append("verify_runner")

    def info(self, message: str) -> None:
        self.events.append("info")

    def verify_infra_ready(self) -> None:
        self.events.append("verify_ready")

    def finalize_runner(self) -> None:
        self.events.append("finalize_runner")

    def check_ready(self) -> None:
        self.events.append("check_ready")


def test_check_mode_finishes_temporary_runner() -> None:
    host = ExecuteHarness("check")
    host.execute()
    assert_equal(
        host.events,
        [
            "require_host",
            "verify_runner",
            "info",
            "verify_ready",
            "finalize_runner",
            "check_ready",
        ],
        "--check через публичный bootstrap должен удалить временный 990 после проверки 910",
    )


class ForeignRemoveHarness(BootstrapHost):
    def __init__(self) -> None:
        super().__init__("remove")
        self.events: list[str] = []

    def remove_runner_if_present(self) -> None:
        self.events.append("remove_runner")

    def infra_exists(self) -> bool:
        return True

    def infra_config_is_expected(self) -> bool:
        return False

    def pct(self, *args: str, **kwargs):
        self.events.append("pct:" + " ".join(args))
        raise AssertionError("Разрушительная команда pct не должна выполняться для чужого 910")


def test_remove_rejects_foreign_910() -> None:
    host = ForeignRemoveHarness()
    try:
        host.remove_infra()
    except BootstrapError:
        pass
    else:
        raise AssertionError("--remove должен отказаться удалять чужой VMID 910")

    assert_equal(
        host.events,
        ["remove_runner"],
        "После обнаружения чужого 910 не должно быть разрушительных действий над ним",
    )


def main() -> None:
    tests = [
        test_strict_ownership_marker,
        test_new_install_flow,
        test_existing_without_bootstrap_state,
        test_resume_unfinished_initial_state,
        test_recover_existing_without_state,
        test_recover_unfinished_initial_state,
        test_check_mode_finishes_temporary_runner,
        test_remove_rejects_foreign_910,
    ]
    for test in tests:
        test()
    print(f"[ОК] Проверки bootstrap-host.py пройдены: {len(tests)}")


if __name__ == "__main__":
    main()
