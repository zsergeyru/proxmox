#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

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


class PveHelperHarness(BootstrapHost):
    def __init__(
        self,
        *,
        token_payload: str = '{"value":"test-secret"}',
        addresses: str = "192.168.1.152 STREAM pve\n",
    ) -> None:
        super().__init__("apply")
        self.token_payload = token_payload
        self.addresses = addresses
        self.events: list[tuple[str, ...] | str] = []

    def run(self, *args: str, **kwargs):
        del kwargs
        command = tuple(args)
        self.events.append(command)

        if command[:5] == (
            "pveum",
            "user",
            "token",
            "add",
            "root@pam",
        ):
            return SimpleNamespace(
                returncode=0,
                stdout=self.token_payload,
                stderr="",
            )
        if command == ("hostname", "-s"):
            return SimpleNamespace(returncode=0, stdout="pve\n", stderr="")
        if command == ("getent", "ahostsv4", "pve"):
            return SimpleNamespace(
                returncode=0,
                stdout=self.addresses,
                stderr="",
            )
        raise AssertionError(f"Неожиданная команда: {command!r}")

    def remove_named_token(self, user: str, token_name: str) -> None:
        self.events.append(f"remove:{user}!{token_name}")


def test_full_pve_token_contract() -> None:
    host = PveHelperHarness()
    token_id, secret = host.create_full_pve_token("infra-manager")

    assert_equal(
        (token_id, secret),
        ("root@pam!infra-manager", "test-secret"),
        "Должны возвращаться полный token ID и secret",
    )
    expected = (
        "pveum",
        "user",
        "token",
        "add",
        "root@pam",
        "infra-manager",
        "--privsep",
        "0",
        "--output-format",
        "json",
    )
    if expected not in host.events:
        raise AssertionError(
            "Полный PVE token должен создаваться явно с --privsep 0"
        )


def test_full_pve_token_missing_secret_is_removed() -> None:
    host = PveHelperHarness(token_payload="{}")
    try:
        host.create_full_pve_token("infra-manager")
    except BootstrapError:
        pass
    else:
        raise AssertionError("Token без secret должен считаться ошибкой")

    if "remove:root@pam!infra-manager" not in host.events:
        raise AssertionError("Token без secret должен быть сразу удалён")


def test_full_pve_token_invalid_json_is_removed() -> None:
    host = PveHelperHarness(token_payload="{invalid")
    try:
        host.create_full_pve_token("bootstrap-runner")
    except BootstrapError:
        pass
    else:
        raise AssertionError("Некорректный JSON PVE должен считаться ошибкой")

    if "remove:root@pam!bootstrap-runner" not in host.events:
        raise AssertionError("Token после некорректного ответа PVE должен быть удалён")


def test_pve_node_address() -> None:
    host = PveHelperHarness()
    assert_equal(
        host.pve_node_address(),
        ("pve", "192.168.1.152"),
        "Должны определяться имя и первый IPv4 PVE-узла",
    )

    missing = PveHelperHarness(addresses="")
    try:
        missing.pve_node_address()
    except BootstrapError:
        pass
    else:
        raise AssertionError("Отсутствие IPv4 PVE должно считаться ошибкой")


class ProgressHarness(BootstrapHost):
    def __init__(self, log_file: Path) -> None:
        super().__init__("apply")
        self.log_file = log_file
        self.events: list[str] = []

    def info(self, message: str) -> None:
        self.events.append(message)


def test_progress_streaming() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        log_file = Path(tmp) / "bootstrap.log"
        host = ProgressHarness(log_file)
        result = host.run(
            sys.executable,
            "-c",
            (
                'print("TASK [Собрать infra-runtime] ************************")\n'
                'print("PLAY RECAP ****************************************")'
            ),
            quiet=True,
            progress=True,
        )

        assert_equal(result.returncode, 0, "Потоковый запуск должен завершаться успешно")
        assert_equal(
            host.events,
            [
                "Ansible: Собрать infra-runtime",
                "Ansible: формирование итогов",
            ],
            "В основной консоли должны появляться названия долгих Ansible-задач",
        )
        log_text = log_file.read_text(encoding="utf-8")
        if "TASK [Собрать infra-runtime]" not in log_text or "PLAY RECAP" not in log_text:
            raise AssertionError("Полный вывод должен одновременно сохраняться в журнале")


class PhaseProgressHarness(BootstrapHost):
    def __init__(self) -> None:
        super().__init__("apply")
        self.calls: list[dict[str, object]] = []

    def log(self, message: str) -> None:
        del message

    def ok(self, message: str) -> None:
        del message

    def ct_exec(self, *args: str, **kwargs):
        del args
        self.calls.append(kwargs)
        return SimpleNamespace(returncode=0)


def test_ansible_phases_enable_progress() -> None:
    host = PhaseProgressHarness()

    host.deploy_910_phase("infrastructure", "infra", "ok")
    host.deploy_910_phase("base", "base", "ok")
    host.deploy_910_phase("provision", "full", "ok")
    host.deploy_910_phase("existing", "existing", "ok")

    assert_equal(
        [call.get("progress") for call in host.calls],
        [False, True, True, True],
        "Ansible-фазы должны показывать потоковый прогресс",
    )
    if not all(call.get("quiet") is True for call in host.calls):
        raise AssertionError("Подробный вывод должен продолжать сохраняться в журнале")


class SemaphoreAccessHarness(BootstrapHost):
    def __init__(self) -> None:
        super().__init__("apply")
        self.commands: list[tuple[str, ...]] = []

    def log(self, message: str) -> None:
        self.commands.append(("log", message))

    def infra_test(self, flag: str, path: str) -> bool:
        if path.endswith("initial-admin-password"):
            return True
        raise AssertionError(f"Неожиданная проверка: {flag} {path}")

    def infra_exec(self, *args: str, **kwargs):
        del kwargs
        command = tuple(args)
        self.commands.append(command)
        if command == ("hostname", "-I"):
            return SimpleNamespace(returncode=0, stdout="192.168.9.10 \n", stderr="")
        if command == (
            "git",
            "-C",
            "/var/lib/infra-manager/bootstrap-repo",
            "rev-parse",
            "--short",
            "HEAD",
        ):
            return SimpleNamespace(returncode=0, stdout="abc1234\n", stderr="")
        if command == (
            "cat",
            "/etc/infra-manager/secrets/initial-admin-password",
        ):
            return SimpleNamespace(returncode=0, stdout="secret-pass\n", stderr="")
        raise AssertionError(f"Неожиданная команда: {command!r}")


def test_installation_summary_is_shown_every_time() -> None:
    import contextlib
    import io

    host = SemaphoreAccessHarness()
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        host.show_installation_summary()
        host.show_installation_summary()

    text_output = output.getvalue()
    if text_output.count("910 infra-manager полностью готов") != 2:
        raise AssertionError("Итоговый блок должен выводиться при каждом запуске")
    if text_output.count("http://192.168.9.10:3000") != 2:
        raise AssertionError("Адрес Semaphore должен выводиться при каждом запуске")
    if text_output.count("Логин:   admin") != 2:
        raise AssertionError("Логин Semaphore должен выводиться при каждом запуске")
    if text_output.count("Пароль:  secret-pass") != 2:
        raise AssertionError("Первичный пароль должен выводиться при каждом запуске")
    if text_output.count("Ветка:   feature/bootstrap-990") != 2:
        raise AssertionError("В итоговом блоке должна выводиться ветка проекта")
    if text_output.count("Версия:  abc1234") != 2:
        raise AssertionError("В итоговом блоке должна выводиться версия проекта")
    password_reads = [
        command
        for command in host.commands
        if command == (
            "cat",
            "/etc/infra-manager/secrets/initial-admin-password",
        )
    ]
    if len(password_reads) != 2:
        raise AssertionError("Пароль должен читаться при каждом выводе доступа")


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

    def ensure_runner_ssh_access_to_infra(self) -> None:
        self.events.append("ensure_runner_ssh")

    def deploy_910_phase(self, phase: str, title: str, success: str) -> None:
        del title, success
        self.events.append(f"deploy:{phase}")

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
            "ensure_runner_ssh",
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
            "pve_access:apply",
            "handoff_existing",
            "ensure_runner_ssh",
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
    if "ensure_runner_ssh" not in host.events:
        raise AssertionError(
            "Перед Ansible незавершённая установка должна восстановить SSH-доступ 990"
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
            "ensure_runner_ssh",
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
        test_full_pve_token_contract,
        test_full_pve_token_missing_secret_is_removed,
        test_full_pve_token_invalid_json_is_removed,
        test_pve_node_address,
        test_progress_streaming,
        test_ansible_phases_enable_progress,
        test_installation_summary_is_shown_every_time,
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
