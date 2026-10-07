#!/usr/bin/env python3
"""Проверки минимальной PVE-оболочки и операторской логики 910."""

from __future__ import annotations

import contextlib
import importlib.util
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = ROOT / "scripts" / "infra-manager"
sys.path.insert(0, str(MODULE_ROOT))

HOST_TARGET = ROOT / "scripts" / "infra-manager" / "host" / "manager.py"
spec = importlib.util.spec_from_file_location(
    "infra_manager_host_manager",
    HOST_TARGET,
)
if spec is None or spec.loader is None:
    raise SystemExit(f"Не удалось загрузить {HOST_TARGET}")
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)

from infra_manager import operator


def test_host_proxy() -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object):
        del kwargs
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with (
        patch.object(
            host,
            "manager_identity",
            return_value=(920, "infra-manager"),
        ),
        patch.object(host, "verify_manager") as verify,
        patch.object(host, "guest_running", return_value=True),
        patch.object(host.socket, "gethostname", return_value="pve.example"),
        patch.object(host, "run", side_effect=fake_run),
    ):
        assert host.proxy_to_manager(["status", "410"]) == 0

    verify.assert_called_once_with(920, "infra-manager")
    assert calls == [[
        "pct",
        "exec",
        "920",
        "--",
        "env",
        "INFRA_PVE_NODE=pve",
        "/usr/local/sbin/infra-manager",
        "--trusted-pve",
        "status",
        "410",
    ]]


def test_host_repair_starts_manager() -> None:
    with (
        patch.object(
            host,
            "manager_identity",
            return_value=(920, "infra-manager"),
        ),
        patch.object(host, "verify_manager") as verify,
        patch.object(host, "start_manager") as start,
        patch.object(host.socket, "gethostname", return_value="pve.example"),
        patch.object(
            host,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
        ) as run,
    ):
        assert host.proxy_to_manager(["repair"]) == 0

    verify.assert_called_once_with(920, "infra-manager")
    start.assert_called_once_with(920)
    assert run.call_args.args[0][-2:] == ["--trusted-pve", "repair"]


def test_host_refuses_stopped_manager_for_regular_commands() -> None:
    with (
        patch.object(
            host,
            "manager_identity",
            return_value=(920, "infra-manager"),
        ),
        patch.object(host, "verify_manager"),
        patch.object(host, "guest_running", return_value=False),
    ):
        try:
            host.proxy_to_manager(["guests"])
        except host.OperatorError as exc:
            assert "repair" in str(exc)
            assert "recover" in str(exc)
        else:
            raise AssertionError("Остановленный manager должен блокировать proxy")


def test_host_verifies_manager_ownership() -> None:
    good = SimpleNamespace(
        returncode=0,
        stdout=(
            "hostname: infra-manager\n"
            "unprivileged: 1\n"
            "description: [owner=proxmox-project;role=infra-manager]\n"
        ),
        stderr="",
    )
    with patch.object(host, "run", return_value=good):
        host.verify_manager(920, "infra-manager")

    bad = SimpleNamespace(
        returncode=0,
        stdout="unprivileged: 1\ndescription: чужой объект\n",
        stderr="",
    )
    with patch.object(host, "run", return_value=bad):
        try:
            host.verify_manager(920, "infra-manager")
        except host.OperatorError as exc:
            assert "recover" in str(exc)
        else:
            raise AssertionError("Чужой VMID нельзя использовать как infra-manager")


def test_host_recover_uses_only_recovery_helper() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        recovery = Path(tmp) / "infra-manager-recovery"
        recovery.write_text("#!/bin/sh\n", encoding="utf-8")
        recovery.chmod(0o700)

        with (
            patch.object(host, "RECOVERY_COMMAND", recovery),
            patch.object(
                host,
                "run",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout="",
                    stderr="",
                ),
            ) as run,
        ):
            assert host.recover() == 0

    run.assert_called_once_with(
        [str(recovery), "--recover"],
        check=False,
    )


def test_host_wrapper_is_minimal() -> None:
    source = HOST_TARGET.read_text(encoding="utf-8")
    forbidden = (
        "Semaphore",
        "docker compose",
        "BOOTSTRAP_URL",
        "urllib.",
        "OPENBAO_COMMAND",
        "ACTIVATE_RUNTIME",
        "project_branch",
    )
    for fragment in forbidden:
        if fragment in source:
            raise AssertionError(
                f"PVE-оболочка содержит лишнюю рабочую логику: {fragment}"
            )

    if source.count("\n") > 150:
        raise AssertionError("PVE-оболочка стала слишком большой")
    if '"pct",\n            "exec"' not in source:
        raise AssertionError("PVE-оболочка должна проксировать через pct exec")
    if '"--recover"' not in source:
        raise AssertionError("PVE-оболочка должна сохранять аварийный recover")


def test_operator_guest_task_secret_policy() -> None:
    snapshot = "/tmp/infra-manager-direct-test-410"
    for operation, expect_status in (
        ("deploy", True),
        ("sync", True),
        ("repair", True),
        ("test", False),
    ):
        with (
            patch.object(
                operator,
                "_prepare_runtime_snapshot",
                return_value=snapshot,
            ) as prepare_snapshot,
            patch.object(
                operator,
                "run",
                return_value=SimpleNamespace(returncode=0),
            ) as runtime_run,
            patch.object(
                operator,
                "operator_guest_status",
                return_value=0,
            ) as status,
        ):
            assert operator.operator_guest_task(
                operation,
                410,
                show_secrets=True,
            ) == 0

        prepare_snapshot.assert_called_once_with(operation, 410)
        runtime_run.assert_called_once_with(
            [
                "docker",
                "exec",
                "--user",
                "1001:0",
                operator.RUNTIME_CONTAINER,
                "sh",
                "-eu",
                "-c",
                operator.RUNTIME_DIRECT_SCRIPT,
                "infra-manager-direct",
                snapshot,
                operation,
                "410",
            ],
            check=False,
            stream_output=True,
        )
        if expect_status:
            status.assert_called_once_with(410, show_secrets=True)
        else:
            status.assert_not_called()

    with (
        patch.object(
            operator,
            "_prepare_runtime_snapshot",
            return_value=snapshot,
        ),
        patch.object(
            operator,
            "run",
            return_value=SimpleNamespace(returncode=7),
        ),
        patch.object(operator, "operator_guest_status") as status,
        patch.object(operator, "console") as console,
    ):
        assert operator.operator_guest_task(
            "deploy",
            410,
            show_secrets=True,
        ) == 7
    status.assert_not_called()
    console.error.assert_called_once_with(
        "Операция deploy для гостя 410 завершилась с кодом 7"
    )

    with (
        patch.object(
            operator,
            "_prepare_runtime_snapshot",
            return_value="/tmp/infra-manager-direct-test-910",
        ),
        patch.object(
            operator,
            "run",
            return_value=SimpleNamespace(returncode=0),
        ),
        patch.object(
            operator,
            "guest_identity",
            return_value=SimpleNamespace(
                vmid=910,
                name="infra-manager",
                role=operator.SETTINGS.infra_manager_role,
            ),
        ) as identity,
        patch.object(operator, "operator_guest_status") as status,
        patch.object(operator, "console") as console,
    ):
        assert operator.operator_guest_task(
            "deploy",
            910,
            show_secrets=True,
        ) == 0

    identity.assert_called_once_with(operator.PATHS.repo_root, 910)
    status.assert_not_called()
    console.info.assert_called_once_with(
        "Отложенная активация infra-runtime продолжится "
        "после завершения команды"
    )


def test_internal_project_refresh_uses_existing_checkout() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        root.mkdir()
        (root / ".git").mkdir()
        secret_dir = Path(tmp) / "secrets"
        secret_dir.mkdir()
        github_key = secret_dir / "github"
        github_key.write_text("key\n", encoding="utf-8")
        semaphore_dir = Path(tmp) / "semaphore"
        semaphore_dir.mkdir()
        known_hosts = semaphore_dir / "known_hosts"
        known_hosts.write_text("github.test key\n", encoding="utf-8")

        paths = SimpleNamespace(
            repo_root=root,
            github_key=github_key,
            semaphore_dir=semaphore_dir,
        )
        calls: list[tuple[list[str], dict[str, object]]] = []

        def fake_run(argv: list[str], **kwargs: object):
            calls.append((argv, kwargs))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch.object(operator, "PATHS", paths),
            patch.object(
                operator,
                "SETTINGS",
                SimpleNamespace(
                    project_branch=lambda: "feature/test",
                ),
            ),
            patch.object(operator, "run", side_effect=fake_run),
            patch.object(
                operator,
                "project_revision",
                return_value="abc1234",
            ),
        ):
            operator._refresh_project_checkout()

    assert [call[0] for call in calls] == [
        [
            "git",
            "-C",
            str(root),
            "fetch",
            "--depth",
            "1",
            "origin",
            "feature/test",
        ],
        [
            "git",
            "-C",
            str(root),
            "checkout",
            "-f",
            "-B",
            "feature/test",
            "FETCH_HEAD",
        ],
        ["git", "-C", str(root), "clean", "-ffdx"],
    ]
    environment = calls[0][1]["env"]
    assert str(github_key) in environment["GIT_SSH_COMMAND"]
    assert str(known_hosts) in environment["GIT_SSH_COMMAND"]


def test_runtime_snapshot_refresh_policy() -> None:
    snapshot_run = SimpleNamespace(returncode=0)
    with (
        patch.object(
            operator,
            "RUNTIME_GUEST_OPERATION",
            SimpleNamespace(is_file=lambda: True),
        ),
        patch.object(
            operator,
            "project_checkout_lock",
            return_value=contextlib.nullcontext(),
        ) as checkout_lock,
        patch.object(operator, "_refresh_project_checkout") as refresh,
        patch.object(operator, "run", return_value=snapshot_run) as run_command,
        patch.object(operator.os, "getpid", return_value=1234),
    ):
        snapshot = operator._prepare_runtime_snapshot("deploy", 410)

    assert snapshot == "/tmp/infra-manager-direct-1234-410"
    checkout_lock.assert_called_once_with(exclusive=True)
    refresh.assert_called_once_with()
    run_command.assert_called_once_with(
        [
            "docker",
            "exec",
            "--user",
            "1001:0",
            operator.RUNTIME_CONTAINER,
            "sh",
            "-eu",
            "-c",
            operator.RUNTIME_SNAPSHOT_SCRIPT,
            "infra-manager-snapshot",
            snapshot,
            operator.RUNTIME_PROJECT_ROOT,
        ]
    )

    with (
        patch.object(
            operator,
            "RUNTIME_GUEST_OPERATION",
            SimpleNamespace(is_file=lambda: True),
        ),
        patch.object(
            operator,
            "project_checkout_lock",
            return_value=contextlib.nullcontext(),
        ) as checkout_lock,
        patch.object(operator, "_refresh_project_checkout") as refresh,
        patch.object(operator, "run", return_value=snapshot_run),
        patch.object(operator.os, "getpid", return_value=1235),
    ):
        snapshot = operator._prepare_runtime_snapshot("sync", 420)

    assert snapshot == "/tmp/infra-manager-direct-1235-420"
    checkout_lock.assert_called_once_with(exclusive=False)
    refresh.assert_not_called()


def test_operator_status_secret_policy() -> None:
    import infra_manager.status as status_module

    with (
        patch.object(status_module, "check_status", return_value=0) as check,
        patch.object(operator, "_pve_node", return_value="pve"),
        patch.object(
            operator,
            "show_openbao_operator_credentials",
        ) as credentials,
    ):
        assert operator.operator_status(show_secrets=False) == 0
        credentials.assert_not_called()

        assert operator.operator_status(show_secrets=True) == 0

    check.assert_called_with(full=True, quiet=False)
    credentials.assert_called_once_with("pve")


def test_operator_prefers_explicit_pve_node() -> None:
    with (
        patch.dict(operator.os.environ, {"INFRA_PVE_NODE": "pve"}, clear=False),
        patch.object(operator, "PveClient") as client,
    ):
        assert operator._pve_node() == "pve"
    client.assert_not_called()


def test_operator_repair_runs_inside_manager() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        compose = root / "docker-compose.yml"
        versions = root / ".versions.env"
        activate = root / "infra-manager-activate-runtime"
        for path in (compose, versions, activate):
            path.write_text("test\n", encoding="utf-8")

        calls: list[tuple[list[str], dict[str, object]]] = []

        def fake_run(argv: list[str], **kwargs: object):
            calls.append((argv, kwargs))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch.object(operator, "COMPOSE_FILE", compose),
            patch.object(operator, "VERSIONS_FILE", versions),
            patch.object(operator, "ACTIVATE_RUNTIME", activate),
            patch.object(operator, "_pve_node", return_value="pve"),
            patch.object(operator, "run", side_effect=fake_run),
            patch.object(
                operator,
                "preflight_recovery_contour",
            ) as preflight,
            patch.object(operator, "repair_openbao_on_host") as repair_openbao,
            patch.object(
                operator,
                "project_branch_for_checkout",
                return_value="feature/test",
            ),
            patch.object(operator, "operator_status", return_value=0) as status,
        ):
            assert operator.operator_repair(show_secrets=True) == 0

    preflight.assert_called_once_with("pve")
    repair_openbao.assert_called_once_with("pve")
    status.assert_called_once_with(show_secrets=True)
    assert ["systemctl", "start", "docker"] in [call[0] for call in calls]
    assert any(call[0][-1] == "openbao" for call in calls)
    activation = next(call for call in calls if call[0] == [str(activate)])
    environment = activation[1]["env"]
    assert environment["INFRA_PVE_NODE"] == "pve"
    assert environment["INFRA_PROJECT_BRANCH"] == "feature/test"


def test_public_command_contract() -> None:
    parser = operator.build_parser()
    for argv in (
        ["guests"],
        ["status"],
        ["status", "410"],
        ["deploy", "410"],
        ["sync", "410"],
        ["repair"],
        ["repair", "410"],
        ["test", "410"],
        ["recover"],
    ):
        parser.parse_args(argv)

    try:
        parser.parse_args(["update"])
    except SystemExit:
        pass
    else:
        raise AssertionError("Публичная команда update должна быть удалена")


def main() -> None:
    test_host_proxy()
    test_host_repair_starts_manager()
    test_host_refuses_stopped_manager_for_regular_commands()
    test_host_verifies_manager_ownership()
    test_host_recover_uses_only_recovery_helper()
    test_host_wrapper_is_minimal()
    test_operator_guest_task_secret_policy()
    test_internal_project_refresh_uses_existing_checkout()
    test_runtime_snapshot_refresh_policy()
    test_operator_status_secret_policy()
    test_operator_prefers_explicit_pve_node()
    test_operator_repair_runs_inside_manager()
    test_public_command_contract()
    print("[ОК] Минимальная PVE-оболочка и операторский слой 910 проверены")


if __name__ == "__main__":
    main()
