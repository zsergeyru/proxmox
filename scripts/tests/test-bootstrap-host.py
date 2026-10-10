#!/usr/bin/env python3
"""Проверки закрытого первоначального загрузчика без рабочего сервера PVE.

Проверяются состояния новой установки и восстановления, принадлежность
объектов, передача временного доступа, защита от одновременных запусков
и последовательность вызовов. Для разрушительных команд используются
испытательные классы и временные каталоги; действующий PVE не изменяется.
"""
from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
    """Сравнить фактическое и ожидаемое значение с пояснением для случая ошибки."""
    if actual != expected:
        raise AssertionError(f"{message}\nОжидалось: {expected!r}\nПолучено: {actual!r}")


def test_infra_manager_role_can_move_to_another_vmid() -> None:
    """Убедиться, что выбор роли не привязан к заранее выбранному VMID."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        guest_dir = root / "infrastructure/guests/920-infra-manager"
        guest_dir.mkdir(parents=True)
        (guest_dir / "guest.yaml").write_text(
            "schema_version: 12\n"
            "vmid: 920\n"
            "name: infra-manager\n"
            "role: infra-manager\n",
            encoding="utf-8",
        )

        vmid, name = module._find_role_guest(root, "infra-manager")
        assert_equal(vmid, 920, "Роль infra-manager не должна зависеть от VMID 910")
        assert_equal(name, "infra-manager", "Имя должно читаться из guest.yaml")


def test_role_lookup_is_identical_in_runner_and_host() -> None:
    """Один и тот же файл ищет роль в обоих режимах без копии алгоритма."""
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        manifest = project / "infrastructure/guests/920-infra-manager/guest.yaml"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            "schema_version: 12\nvmid: 920\nname: infra-manager\n"
            "role: infra-manager\n",
            encoding="utf-8",
        )
        expected = module._find_role_guest(project, "infra-manager")
        helper = ROOT / "scripts/bootstrap-runner/bootstrap_runner/role.py"
        completed = subprocess.run(
            [sys.executable, str(helper), str(project), "infra-manager"],
            text=True, capture_output=True, check=True,
        )
        import json

        result = json.loads(completed.stdout)
        assert_equal(
            (result["vmid"], result["name"]), expected,
            "Поиск в LXC должен использовать ту же реализацию, что и PVE",
        )

        manifest.write_text(
            "schema_version: 12\nvmid: 920\nname: wrong\n"
            "role: infra-manager\n",
            encoding="utf-8",
        )
        assert_equal(
            subprocess.run(
                [sys.executable, str(helper), str(project), "infra-manager"],
                text=True, capture_output=True, check=False,
            ).returncode,
            1,
            "Самостоятельный сценарий должен отвергать неправильное описание",
        )
        try:
            module._find_role_guest(project, "infra-manager")
        except BootstrapError:
            pass
        else:
            raise AssertionError("PVE должен отвергать то же неправильное описание")


class RunnerRoleHarness(BootstrapHost):
    """Подставляет чтение описания роли в временном контейнере."""
    def __init__(self) -> None:
        """Подготовить изолированный испытательный контур вместо реального PVE."""
        original = os.environ.get("PROJECT_DIR")
        os.environ["PROJECT_DIR"] = "/var/lib/bootstrap-runner/project"
        try:
            super().__init__("recover")
        finally:
            if original is None:
                os.environ.pop("PROJECT_DIR", None)
            else:
                os.environ["PROJECT_DIR"] = original

    def ct_exec(self, *args: str, **kwargs):
        """Подменить запуск команды внутри 990 и вернуть результат испытания."""
        assert_equal(args[0], "python3", "Роль должна читаться внутри 990 через Python")
        assert_equal(args[-2], "/var/lib/bootstrap-runner/project", "Неверный путь проекта 990")
        assert_equal(args[-1], "infra-manager", "Неверная искомая роль")
        return SimpleNamespace(
            returncode=0,
            stdout='{"vmid": 920, "name": "infra-manager"}\n',
            stderr="",
        )


def test_role_is_read_from_project_inside_runner() -> None:
    """Проверить поиск роли в закрытой копии проекта временного контейнера."""
    host = RunnerRoleHarness()
    assert_equal(host.infra_ctid, 920, "VMID должен определяться из проекта внутри 990")
    assert_equal(host.infra_hostname, "infra-manager", "Имя должно определяться из проекта внутри 990")


class OwnershipHarness(BootstrapHost):
    """Изолирует проверку владения управляющим гостем."""
    def __init__(self, config: str) -> None:
        """Подготовить изолированный испытательный контур вместо реального PVE."""
        super().__init__("apply")
        self._config = config

    def infra_exists(self) -> bool:
        """Подменить проверку существования управляющего объекта."""
        return True

    def pct_config(self, ctid: int) -> str:
        """Вернуть испытательное описание объекта PVE."""
        if ctid != self.infra_ctid:
            raise AssertionError(f"Неожиданный VMID: {ctid}")
        return self._config


def test_strict_ownership_marker() -> None:
    """Отвергать управляющие объекты с чужими метками или режимом."""
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
        good.replace("owner=proxmox-project", "owner=proxmox-project-fake"),
        good.replace("role=infra-manager", "role=infra-manager-fake"),
        good.replace("role=infra-manager", "role=infra-manager;role=other"),
    )
    for config in variants:
        if OwnershipHarness(config).infra_config_is_expected():
            raise AssertionError(
                "Неполный контракт владения не должен приниматься за infra-manager"
            )


class RunnerOwnershipHarness(BootstrapHost):
    """Проверяет метки владения временным контейнером."""
    def __init__(self, config: str) -> None:
        """Подготовить изолированный испытательный контур вместо реального PVE."""
        super().__init__("apply")
        self._config = config

    def ct_exists(self) -> bool:
        """Подменить проверку наличия временного контейнера."""
        return True

    def pct_config(self, ctid: int) -> str:
        """Вернуть испытательное описание объекта PVE."""
        assert ctid == self.ctid
        return self._config


def test_runner_ownership_requires_exact_markers() -> None:
    """Не принимать чужой временный LXC из-за частичного совпадения меток."""
    valid = ("hostname: bootstrap-runner\n"
             "tags: bootstrap-runner;project\n"
             "description: created [managed-by=proxmox-bootstrap]\n")
    RunnerOwnershipHarness(valid).assert_owned_runner()
    invalid = (
        valid.replace("bootstrap-runner;project", "bootstrap-runner-foreign;project"),
        valid.replace("managed-by=proxmox-bootstrap]", "managed-by=proxmox-bootstrap-fake]"),
        valid.replace("managed-by=proxmox-bootstrap]", "managed-by=proxmox-bootstrap;managed-by=other]"),
    )
    for config in invalid:
        try:
            RunnerOwnershipHarness(config).assert_owned_runner()
        except BootstrapError:
            pass
        else:
            raise AssertionError("Чужой временный контейнер не должен приниматься за 990")


class PveHelperHarness(BootstrapHost):
    """Имитирует команды PVE API и их ответы."""
    def __init__(
        self,
        *,
        token_payload: str = '{"value":"test-secret"}',
        addresses: str = "192.168.1.152 STREAM pve\n",
    ) -> None:
        """Подготовить изолированный испытательный контур вместо реального PVE."""
        super().__init__("apply")
        self.token_payload = token_payload
        self.addresses = addresses
        self.events: list[tuple[str, ...] | str] = []

    def run(self, *args: str, **kwargs):
        """Записать команду или вернуть подставленный результат без доступа к PVE."""
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
        """Записать отзыв токена вместо изменения PVE."""
        self.events.append(f"remove:{user}!{token_name}")


def test_full_pve_token_contract() -> None:
    """Проверить имя, полные права и передачу секрета токена PVE."""
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
    """Удалить созданный токен, если PVE не вернул его секрет."""
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
    """Отозвать токен при повреждённом ответе JSON от PVE."""
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
    """Определить имя и адрес PVE без привязки к известному IP."""
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


def test_bootstrap_timing_success_and_failure() -> None:
    """Проверить время этапа при успехе и ожидаемой ошибке."""
    host = BootstrapHost("apply")
    output = io.StringIO()
    with (
        patch.object(module.time, "monotonic", side_effect=[10.0, 72.4]),
        contextlib.redirect_stdout(output),
    ):
        host.timed_step("Тестовый этап", lambda: host.ok("Тест завершён"))

    if not any(
        "[ОК]" in line and "Тест завершён" in line and line.endswith("(01:02)")
        for line in output.getvalue().splitlines()
    ):
        raise AssertionError("Время должно быть справа в исходной строке [ОК]")
    if "[ВРЕМЯ]" in output.getvalue():
        raise AssertionError("Отдельный вывод [ВРЕМЯ] запрещён")

    output = io.StringIO()
    with (
        patch.object(module.time, "monotonic", side_effect=[10.0, 11.3]),
        contextlib.redirect_stdout(output),
    ):
        result = host.timed_step("Проверка", lambda: "ready")
    assert_equal(result, "ready", "Замер не должен менять результат этапа")
    if not any(
        "[ОК]" in line and "Проверка" in line and line.endswith("(00:01)")
        for line in output.getvalue().splitlines()
    ):
        raise AssertionError("Этап без собственного [ОК] должен получить итоговый статус")

    def fail_operation():
        """Смоделировать отказ внутреннего шага для проверки сообщения об ошибке."""
        raise BootstrapError("ожидаемая ошибка")

    with patch.object(module.time, "monotonic", side_effect=[100.0, 107.5]):
        try:
            host.timed_step("Сбой этапа", fail_operation)
        except BootstrapError as exc:
            if str(exc) != "ожидаемая ошибка (этап «Сбой этапа»: 00:07)":
                raise
        else:
            raise AssertionError("Замер не должен скрывать ошибку этапа")

    if module._format_duration(3661.9) != "01:01:01":
        raise AssertionError("Формат времени должен поддерживать часы")


def test_section_totals_include_interrupted_section() -> None:
    """Завершать прерванный раздел с правильным статусом."""
    host = BootstrapHost("apply")
    output = io.StringIO()
    with (
        patch.object(
            module.time,
            "monotonic",
            side_effect=[10.0, 35.4, 50.0, 61.6],
        ),
        contextlib.redirect_stdout(output),
    ):
        host.log("Подготовка 990")
        host.log("Настройка 910")
        host.finish_section(interrupted=True)
        host.finish_section()

    text = output.getvalue()
    if "[ИТОГ] Подготовка 990" not in text or "(00:25)" not in text:
        raise AssertionError("Раздел должен завершаться строкой с общим временем")
    if "[ПРЕРВАНО] Настройка 910" not in text or "(00:11)" not in text:
        raise AssertionError("Прерванный раздел должен показывать прошедшее время")
    if text.count("[ИТОГ]") != 1 or text.count("[ПРЕРВАНО]") != 1:
        raise AssertionError("Итог раздела нельзя печатать повторно")


class ProgressHarness(BootstrapHost):
    """Запоминает сообщения потока задач Ansible."""
    def __init__(self, log_file: Path) -> None:
        """Подготовить изолированный испытательный контур вместо реального PVE."""
        super().__init__("apply")
        self.log_file = log_file
        self.events: list[str] = []

    def info(self, message: str) -> None:
        """Сохранить диагностическое событие для проверки порядка действий."""
        self.events.append(message)


def test_progress_streaming() -> None:
    """Сохранить полный журнал и краткие сообщения долгих задач."""
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
        if "[ВРЕМЯ] Ansible: Собрать infra-runtime:" not in log_text:
            raise AssertionError("Журнал должен сохранять длительность каждой Ansible-задачи")


class BootstrapStepHarness(BootstrapHost):
    """Имитирует внутренние шаги первоначального развёртывания."""
    def __init__(self) -> None:
        """Подготовить изолированный испытательный контур вместо реального PVE."""
        super().__init__("apply")
        self.calls: list[dict[str, object]] = []

    def log(self, message: str) -> None:
        """Сохранить начало испытательного этапа."""
        del message

    def ok(self, message: str) -> None:
        """Сохранить сообщение об успешном испытательном действии."""
        del message

    def ct_exec(self, *args: str, **kwargs):
        """Подменить запуск команды внутри 990 и вернуть результат испытания."""
        del args
        self.calls.append(kwargs)
        return SimpleNamespace(returncode=0)


def test_bootstrap_steps_enable_progress() -> None:
    """Передавать правильный режим отображения внутренним этапам."""
    host = BootstrapStepHarness()

    host.create_infra_manager()
    host.configure_infra_manager_base()
    host.configure_infra_manager()

    assert_equal(
        [call.get("progress") for call in host.calls],
        [False, None, True, True],
        "Create должен сразу очищать state, а Ansible-шаги показывать прогресс",
    )
    if not all(call.get("quiet") is True for call in host.calls):
        raise AssertionError("Подробный вывод должен продолжать сохраняться в журнале")


class InfraReadyHarness(BootstrapHost):
    """Подставляет результат проверки готовности гостя."""
    def __init__(self) -> None:
        """Подготовить изолированный испытательный контур вместо реального PVE."""
        super().__init__("apply")
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def verify_infra_object(self) -> None:
        """Не обращаться к PVE при проверке поведения вызывающего метода."""
        return

    def infra_test(self, flag: str, path: str) -> bool:
        """Вернуть испытательное состояние файла внутри гостя."""
        return flag == "-x" and path == "/usr/local/sbin/infra-manager-status"

    def infra_exec(self, *args: str, **kwargs):
        """Проверить переданные аргументы без вызова команд в госте."""
        self.calls.append((tuple(args), dict(kwargs)))
        return SimpleNamespace(returncode=0)


def test_infra_ready_uses_status_as_final_screen() -> None:
    """Использовать полную проверку состояния как критерий готовности."""
    host = InfraReadyHarness()

    host.verify_infra_ready(quiet=True)
    host.verify_infra_ready()

    quiet_args, quiet_kwargs = host.calls[0]
    visible_args, visible_kwargs = host.calls[1]

    if quiet_args[-2:] != ("--full", "--quiet"):
        raise AssertionError("Предварительная проверка должна скрывать итоговый экран")
    if quiet_kwargs.get("quiet") is not True:
        raise AssertionError("Предварительная проверка не должна попадать в консоль")

    if visible_args[-1:] != ("--full",) or "--quiet" in visible_args:
        raise AssertionError("Финальная проверка должна показывать полный экран состояния")
    if visible_kwargs.get("quiet") is not False:
        raise AssertionError("Финальный экран состояния должен выводиться в консоль")


class PersistentAttachHarness(BootstrapHost):
    """Имитирует PVE при подключении постоянных каталогов."""
    def __init__(self, *, fail_mount: bool = False) -> None:
        """Подготовить изолированный испытательный контур вместо реального PVE."""
        super().__init__("apply")
        self.fail_mount = fail_mount
        self.running = True
        self.events: list[str] = []

    def infra_exists(self) -> bool:
        """Подменить проверку существования управляющего объекта."""
        self.events.append("infra_exists")
        return True

    def infra_config_is_expected(self) -> bool:
        """Вернуть ожидаемый результат проверки принадлежности."""
        self.events.append("infra_expected")
        return True

    def pct_config(self, ctid: int) -> str:
        """Вернуть испытательное описание объекта PVE."""
        if ctid != self.infra_ctid:
            raise AssertionError(f"Неожиданный VMID: {ctid}")
        self.events.append("pct_config")
        return "protection: 1\n"

    def pct_status(self, ctid: int) -> str:
        """Вернуть испытательное состояние контейнера."""
        if ctid != self.infra_ctid:
            raise AssertionError(f"Неожиданный VMID: {ctid}")
        self.events.append("status")
        return "running" if self.running else "stopped"

    def pct(self, *args: str, **kwargs):
        """Записать или запретить административную команду в испытании."""
        check = kwargs.get("check", True)
        command = " ".join(args)
        self.events.append(
            f"pct:{command}" + (":nocheck" if check is False else "")
        )
        if args[:2] == ("stop", str(self.infra_ctid)):
            self.running = False
        elif args[:2] == ("start", str(self.infra_ctid)):
            self.running = True
        elif "--mp0" in args and self.fail_mount:
            raise BootstrapError("тестовая ошибка подключения mount point")
        return SimpleNamespace(returncode=0)

    def verify_persistent_layout(self) -> None:
        """Подменить проверку постоянных подключений."""
        self.events.append("verify_layout")

    def ok(self, message: str) -> None:
        """Сохранить сообщение об успешном испытательном действии."""
        del message
        self.events.append("ok")


def test_attach_persistent_layout_temporarily_disables_protection() -> None:
    """Снимать защиту PVE только на время подключения постоянных каталогов."""
    host = PersistentAttachHarness()
    host.attach_persistent_layout()

    protection_off = "pct:set 910 --protection 0"
    mount_event = next(event for event in host.events if "--mp0" in event)
    protection_on = "pct:set 910 --protection 1"

    if protection_off not in host.events:
        raise AssertionError("Перед добавлением mount point защита 910 должна сниматься")
    if protection_on not in host.events:
        raise AssertionError("После добавления mount point защита 910 должна возвращаться")
    if host.events.index(protection_off) > host.events.index(mount_event):
        raise AssertionError("Защита должна сниматься до изменения mount point")
    if host.events.index(protection_on) < host.events.index(mount_event):
        raise AssertionError("Защита должна возвращаться после изменения mount point")
    if not host.running:
        raise AssertionError("После успешного подключения 910 должен быть запущен")
    if "verify_layout" not in host.events:
        raise AssertionError("Новая схема должна проверяться после подключения")


def test_attach_persistent_layout_restores_protection_on_failure() -> None:
    """Восстанавливать защиту после неудачного подключения."""
    host = PersistentAttachHarness(fail_mount=True)
    try:
        host.attach_persistent_layout()
    except BootstrapError:
        pass
    else:
        raise AssertionError("Ошибка подключения mount point должна передаваться выше")

    if "pct:set 910 --protection 1" not in host.events:
        raise AssertionError("При ошибке protection=1 должен быть восстановлен")
    if not host.running:
        raise AssertionError("При ошибке ранее запущенный 910 должен быть возвращён в работу")
    if "verify_layout" in host.events:
        raise AssertionError("Неуспешное подключение не должно считаться проверенным")


class RecoveryRootfsHarness(BootstrapHost):
    """Проверяет условия удаления воспроизводимого rootfs."""
    def __init__(self, *, owned: bool = True) -> None:
        """Подготовить изолированный испытательный контур вместо реального PVE."""
        super().__init__("recover")
        self.owned = owned
        self.exists = True
        self.events: list[str] = []

    def infra_exists(self) -> bool:
        """Подменить проверку существования управляющего объекта."""
        return self.exists

    def infra_config_is_expected(self) -> bool:
        """Вернуть ожидаемый результат проверки принадлежности."""
        return self.owned

    def pct_status(self, ctid: int) -> str:
        """Вернуть испытательное состояние контейнера."""
        if ctid != self.infra_ctid:
            raise AssertionError(f"Неожиданный VMID: {ctid}")
        return "running"

    def pct(self, *args: str, **kwargs):
        """Записать или запретить административную команду в испытании."""
        del kwargs
        self.events.append("pct:" + " ".join(args))
        return SimpleNamespace(returncode=0)

    def run(self, *args: str, **kwargs):
        """Записать команду или вернуть подставленный результат без доступа к PVE."""
        del kwargs
        self.events.append("run:" + " ".join(args))
        if args[:2] == ("pct", "destroy"):
            self.exists = False
        return SimpleNamespace(returncode=0)

    def ok(self, message: str) -> None:
        """Сохранить сообщение об успешном испытательном действии."""
        del message


def test_recovery_rootfs_removal_is_strict() -> None:
    """Удалять при восстановлении только строго принадлежащий проекту объект."""
    host = RecoveryRootfsHarness()
    host.remove_infra_rootfs_for_recovery()
    if not any("--protection 0" in event for event in host.events):
        raise AssertionError("Recovery должен снять protection перед удалением rootfs")
    if not any(event.startswith("pct:stop ") for event in host.events):
        raise AssertionError("Recovery должен остановить работающий infra-manager")
    if not any("pct destroy" in event and "--purge 1" in event for event in host.events):
        raise AssertionError("Recovery должен удалить только объект/rootfs infra-manager")

    foreign = RecoveryRootfsHarness(owned=False)
    try:
        foreign.remove_infra_rootfs_for_recovery()
    except BootstrapError:
        pass
    else:
        raise AssertionError("Recovery не должен удалять чужой объект")
    if foreign.events:
        raise AssertionError("До проверки владения разрушительные команды запрещены")


class ControlPlaneHarness(BootstrapHost):
    """Записывает выбранный этап настройки начальных служб."""
    def __init__(self, mode: str) -> None:
        """Подготовить изолированный испытательный контур вместо реального PVE."""
        super().__init__(mode)
        self.steps: list[str] = []

    def log(self, message: str) -> None:
        """Сохранить начало испытательного этапа."""
        del message

    def ok(self, message: str) -> None:
        """Сохранить сообщение об успешном испытательном действии."""
        del message

    def _run_infra_bootstrap_step(self, step: str, *, progress: bool) -> None:
        """Записать внутренний этап вместо настоящего развёртывания."""
        if not progress:
            raise AssertionError("Управляющий этап должен показывать прогресс")
        self.steps.append(step)


def test_control_plane_step_depends_on_bootstrap_mode() -> None:
    """Использовать отдельный начальный этап при восстановлении."""
    fresh = ControlPlaneHarness("apply")
    fresh.configure_infra_manager_control_plane()
    assert_equal(
        fresh.steps,
        ["configure-control-plane"],
        "Новая установка должна подготовить первичные секреты до OpenBao",
    )

    recovery = ControlPlaneHarness("recover")
    recovery.configure_infra_manager_control_plane()
    assert_equal(
        recovery.steps,
        ["configure-recovery-control-plane"],
        "Recovery не должен запускать Semaphore до восстановления OpenBao",
    )


class ApplyHarness(BootstrapHost):
    """Имитирует все этапы первоначального создания или восстановления."""
    def __init__(
        self,
        mode: str,
        *,
        infra_exists: bool,
    ) -> None:
        """Подготовить изолированный испытательный контур вместо реального PVE."""
        super().__init__(mode)
        self._infra_exists = infra_exists
        self.events: list[str] = []
        # Тесты не должны зависеть от каталогов реального PVE.
        self._test_data = tempfile.TemporaryDirectory()
        self.host_persistent_root = Path(self._test_data.name) / "infra-manager"
        self._test_marker_state: str | None = None

    def installation_state(self, *, guest_exists: bool) -> str:
        """Вернуть испытательный класс состояния установки."""
        if self._test_marker_state == "installing":
            return "unfinished"
        return super().installation_state(guest_exists=guest_exists)

    def write_installation_marker(self, state: str) -> None:
        """Сохранить состояние маркера в памяти испытания."""
        self._test_marker_state = state

    def verify_unfinished_installation(self, *, guest_exists: bool) -> None:
        """Записать предварительную проверку прерванной установки."""
        self.events.append("verify_unfinished")

    def verify_recovery_state(self) -> None:
        """Подменить проверку постоянных данных."""
        self.events.append("verify_recovery_state")

    def infra_exists(self) -> bool:
        """Подменить проверку существования управляющего объекта."""
        self.events.append("infra_exists")
        return self._infra_exists

    def prepare_new_persistent_layout(self) -> None:
        """Зарегистрировать подготовку каталогов без изменения PVE."""
        self.events.append("prepare_new_layout")

    def verify_persistent_layout(self) -> None:
        """Подменить проверку постоянных подключений."""
        self.events.append("verify_layout")

    def attach_persistent_layout(self) -> None:
        """Зарегистрировать подключение каталогов без изменения PVE."""
        self.events.append("attach_layout")

    def prepare_runner(self) -> None:
        """Зарегистрировать подготовку временного контейнера."""
        self.events.append("prepare_runner")

    def remove_infra_rootfs_for_recovery(self) -> None:
        """Смоделировать удаление воспроизводимого объекта."""
        self.events.append("remove_rootfs")
        self._infra_exists = False

    def create_infra_manager(self) -> None:
        """Смоделировать создание управляющего контейнера."""
        self.events.append("create_infra")
        self._infra_exists = True

    def configure_infra_manager_base(self) -> None:
        """Зарегистрировать базовую настройку ОС."""
        self.events.append("configure_base")

    def configure_infra_manager_control_plane(self) -> None:
        """Зарегистрировать настройку начальных служб."""
        self.events.append("configure_control_plane")

    def configure_infra_manager_ssh_trust(self) -> None:
        """Зарегистрировать переход к сертификатному SSH."""
        self.events.append("configure_ssh_trust")

    def sync_infra_ssh_ca_to_runner(self) -> None:
        """Зарегистрировать передачу открытых сертификатов."""
        self.events.append("sync_ssh_ca")

    def remove_bootstrap_ssh_access_from_infra(self) -> None:
        """Зарегистрировать удаление временного SSH-разрешения."""
        self.events.append("remove_bootstrap_auth")

    def configure_infra_manager(self) -> None:
        """Зарегистрировать полную настройку гостя."""
        self.events.append("configure_full")

    def handoff_infra(self, access_mode: str = "apply") -> None:
        """Смоделировать передачу первоначальных учётных данных."""
        self.events.append(f"handoff:{access_mode}")

    def initialize_infra_openbao(self) -> None:
        """Зарегистрировать подготовку OpenBao без реального запуска."""
        self.events.append("initialize_openbao")

    def verify_infra_ready(self, *, quiet: bool = False) -> None:
        """Записать запрос проверки работоспособности."""
        self.events.append("verify_ready:quiet" if quiet else "verify_ready")

    def finalize_runner(self) -> None:
        """Зарегистрировать успешное удаление временного 990."""
        self.events.append("finalize_runner")

    def check_ready(self) -> None:
        """Зафиксировать итоговую проверку отсутствия временного контура."""
        self.events.append("check_ready")

    def info(self, message: str) -> None:
        """Сохранить диагностическое событие для проверки порядка действий."""
        self.events.append("info")


def test_new_install_flow() -> None:
    """Проверить полную последовательность создания на чистом PVE."""
    host = ApplyHarness("apply", infra_exists=False)
    host.apply()
    assert_equal(
        host.events,
        [
            "infra_exists",
            "prepare_new_layout",
            "prepare_runner",
            "create_infra",
            "attach_layout",
            "configure_base",
            "handoff:apply",
            "configure_control_plane",
            "initialize_openbao",
            "sync_ssh_ca",
            "configure_ssh_trust",
            "remove_bootstrap_auth",
            "configure_full",
            "verify_ready:quiet",
            "finalize_runner",
            "check_ready",
        ],
        "Новая установка должна проходить один линейный bootstrap",
    )


def test_auto_recovery_for_absent_infra_and_existing_data() -> None:
    """Не начинать чистую установку поверх прежних данных."""
    host = ApplyHarness("apply", infra_exists=False)
    host.host_persistent_root.mkdir()
    host.apply()
    assert_equal(host.mode, "recover", "Сохранённые данные должны выбирать recovery")
    assert_equal(
        host.events[:4],
        ["infra_exists", "info", "verify_recovery_state", "info"],
        "Проверка сохранённых данных должна предшествовать изменяющим операциям",
    )
    if "prepare_new_layout" in host.events or "handoff:apply" in host.events:
        raise AssertionError("Старые данные нельзя обрабатывать как новую установку")
    if "handoff:recover" not in host.events:
        raise AssertionError("Сохранённые данные требуют режима recovery")


def test_restart_unfinished_installation() -> None:
    """Продолжить прерванную новую установку до появления состояния."""
    host = ApplyHarness("apply", infra_exists=True)
    host._test_marker_state = "installing"
    host.apply()
    assert_equal(host.mode, "apply", "Прерванная новая установка не должна становиться recovery")
    if host.events[:5] != [
        "infra_exists", "verify_unfinished", "info", "prepare_runner", "remove_rootfs"
    ]:
        raise AssertionError(f"Перед пересозданием необходимо проверить состояние: {host.events}")
    if host._test_marker_state != "ready":
        raise AssertionError("Успешная установка должна завершиться маркером ready")


def test_restart_rejects_persistent_data() -> None:
    """Запретить пересоздание поверх заполненного постоянного состояния."""
    with tempfile.TemporaryDirectory() as temp:
        host = BootstrapHost("apply")
        root = Path(temp) / "infra-manager"
        host.host_persistent_root = root
        host.host_pve_only_dir = root / "pve-only"
        host.host_state_dir = root / "state"
        host.host_openbao_unseal_key = root / "pve-only/openbao/unseal.key"
        host.host_pve_only_dir.mkdir(parents=True)
        host.host_state_dir.mkdir(parents=True)
        host.write_installation_marker("installing")
        host.verify_unfinished_installation(guest_exists=False)
        (host.host_state_dir / "semaphore.sqlite").write_bytes(b"data")
        try:
            host.verify_unfinished_installation(guest_exists=False)
        except BootstrapError as exc:
            if "постоянные данные" not in str(exc):
                raise
        else:
            raise AssertionError("Запрещено пересоздавать контейнер с постоянными данными")


class IncompleteAutoRecoveryHarness(ApplyHarness):
    """Имитирует отказ при неполных постоянных данных."""
    def verify_recovery_state(self) -> None:
        """Подменить проверку постоянных данных."""
        self.events.append("verify_recovery_state")
        raise BootstrapError("Recovery запрещён: состояние неполное")


def test_auto_recovery_stops_on_incomplete_data() -> None:
    """Остановить восстановление, если обязательные данные неполны."""
    host = IncompleteAutoRecoveryHarness("apply", infra_exists=False)
    (host.host_persistent_root / "pve-only").mkdir(parents=True)
    try:
        host.apply()
    except BootstrapError as exc:
        if "состояние неполное" not in str(exc):
            raise
    else:
        raise AssertionError("Неполные постоянные каталоги должны блокировать запуск")
    assert_equal(
        host.events,
        ["infra_exists", "info", "verify_recovery_state"],
        "При неполном состоянии запрещены создание 910 и изменение данных",
    )


def test_existing_infra_with_data_is_not_automatically_recreated() -> None:
    """Не пересоздавать существующий объект при обычном запуске."""
    host = ApplyHarness("apply", infra_exists=True)
    host.host_persistent_root.mkdir()
    try:
        host.apply()
    except BootstrapError as exc:
        if "обычный deploy/repair" not in str(exc):
            raise
    else:
        raise AssertionError("Обычный запуск не должен пересоздавать работающий 910")
    assert_equal(
        host.events,
        ["infra_exists", "verify_layout"],
        "Существующий 910 требует явного --recover для пересоздания",
    )


def test_existing_infra_requires_normal_operations() -> None:
    """Направлять обслуживание работающего гостя в обычные операции."""
    host = ApplyHarness("apply", infra_exists=True)
    try:
        host.apply()
    except BootstrapError as exc:
        if "обычный deploy/repair" not in str(exc):
            raise
    else:
        raise AssertionError("990 не должен обслуживать существующий рабочий 910")

    assert_equal(
        host.events,
        [
            "infra_exists",
            "verify_layout",
        ],
        "Обычный сценарий внутри 990 должен остановиться до изменяющих шагов",
    )


def test_recovery_recreates_existing_infra() -> None:
    """Проверить строгий порядок пересоздания существующего гостя."""
    host = ApplyHarness("recover", infra_exists=True)
    host.apply()
    assert_equal(
        host.events,
        [
            "infra_exists",
            "verify_recovery_state",
            "verify_layout",
            "prepare_runner",
            "remove_rootfs",
            "create_infra",
            "attach_layout",
            "configure_base",
            "handoff:recover",
            "configure_control_plane",
            "initialize_openbao",
            "sync_ssh_ca",
            "configure_ssh_trust",
            "remove_bootstrap_auth",
            "configure_full",
            "verify_ready:quiet",
            "finalize_runner",
            "check_ready",
        ],
        "Recovery должен всегда создавать новый rootfs через новый bootstrap-сеанс",
    )


class FailingRunnerHarness(ApplyHarness):
    """Имитирует отказ до удаления существующего гостя."""
    def prepare_runner(self) -> None:
        """Зарегистрировать подготовку временного контейнера."""
        self.events.append("prepare_runner")
        raise BootstrapError("Не удалось подготовить временный 990")


def test_recovery_runner_failure_keeps_existing_910() -> None:
    """Сохранить старый управляющий объект при ошибке подготовки 990."""
    host = FailingRunnerHarness("recover", infra_exists=True)
    try:
        host.apply()
    except BootstrapError as exc:
        if "временный 990" not in str(exc):
            raise
    else:
        raise AssertionError("Ошибка подготовки 990 должна прервать recovery")

    assert_equal(
        host.events,
        ["infra_exists", "verify_recovery_state", "verify_layout", "prepare_runner"],
        "Подготовка 990 должна предшествовать удалению работающего 910",
    )
    if not host._infra_exists:
        raise AssertionError("Существующий 910 нельзя удалять до готовности 990")


def test_recovery_recreates_missing_infra() -> None:
    """Создать отсутствующий объект поверх проверенного сохранённого состояния."""
    host = ApplyHarness("recover", infra_exists=False)
    host.apply()
    assert_equal(
        host.events,
        [
            "infra_exists",
            "verify_recovery_state",
            "info",
            "prepare_runner",
            "create_infra",
            "attach_layout",
            "configure_base",
            "handoff:recover",
            "configure_control_plane",
            "initialize_openbao",
            "sync_ssh_ca",
            "configure_ssh_trust",
            "remove_bootstrap_auth",
            "configure_full",
            "verify_ready:quiet",
            "finalize_runner",
            "check_ready",
        ],
        "Recovery отсутствующего 910 должен использовать тот же новый bootstrap-сеанс",
    )


class RecoveryStateHarness(BootstrapHost):
    """Создаёт временную файловую схему восстановления."""
    def __init__(self, root: Path) -> None:
        """Подготовить изолированный испытательный контур вместо реального PVE."""
        super().__init__("recover")
        self.host_persistent_root = root
        self.host_pve_only_dir = root / "pve-only"
        self.host_access_dir = root / "access"
        self.host_state_dir = root / "state"
        self.host_openbao_dir = self.host_pve_only_dir / "openbao"
        self.host_recovery_dir = self.host_pve_only_dir / "recovery"
        self.host_recovery_github_key = (
            self.host_recovery_dir / "github_proxmox_repo_ed25519"
        )
        self.host_openbao_unseal_key = self.host_openbao_dir / "unseal.key"
        self.host_openbao_ssh_access = self.host_openbao_dir / "ssh-access.json"
        self.host_openbao_kv_access = self.host_openbao_dir / "kv-access.json"
        self.host_state_openbao_dir = self.host_state_dir / "openbao"
        self.host_state_openbao_raft_dir = self.host_state_openbao_dir / "raft"
        self.host_state_opentofu_file = (
            self.host_state_dir / "opentofu" / "state" / "proxmox.tfstate"
        )
        self.host_state_semaphore_db = (
            self.host_state_dir / "semaphore" / "semaphore.sqlite"
        )
        self.messages: list[str] = []

    def ok(self, message: str) -> None:
        """Сохранить сообщение об успешном испытательном действии."""
        self.messages.append(message)

    def managed_guests_exist(self) -> bool:
        """Вернуть испытательный признак существования управляемых гостей."""
        return False


def _prepare_complete_recovery_state(host: RecoveryStateHarness) -> None:
    """Создать имитацию полного постоянного состояния восстановления."""
    for directory in (
        host.host_openbao_dir,
        host.host_recovery_dir,
        host.host_state_openbao_raft_dir,
        host.host_state_opentofu_file.parent,
        host.host_state_semaphore_db.parent,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    for path in (
        host.host_openbao_unseal_key,
        host.host_recovery_github_key,
        host.host_openbao_ssh_access,
        host.host_openbao_kv_access,
        host.host_state_openbao_raft_dir / "raft.db",
        host.host_state_opentofu_file,
        host.host_state_semaphore_db,
    ):
        path.write_text("test\n", encoding="utf-8")


def test_recovery_preflight_accepts_complete_state() -> None:
    """Разрешить восстановление при полном наборе постоянных данных."""
    with tempfile.TemporaryDirectory() as tmp:
        host = RecoveryStateHarness(Path(tmp))
        _prepare_complete_recovery_state(host)
        host.verify_recovery_state()
        if not host.messages:
            raise AssertionError("Полное recovery-состояние должно подтверждаться")


def test_recovery_preflight_allows_missing_approle_files() -> None:
    """Не требовать непостоянные файлы AppRole при восстановлении."""
    with tempfile.TemporaryDirectory() as tmp:
        host = RecoveryStateHarness(Path(tmp))
        _prepare_complete_recovery_state(host)
        host.host_openbao_ssh_access.unlink()
        host.host_openbao_kv_access.unlink()

        host.verify_recovery_state()


def test_recovery_preflight_allows_missing_access_directory() -> None:
    """Проверить допустимость отсутствующих восстанавливаемых доступов."""
    with tempfile.TemporaryDirectory() as tmp:
        host = RecoveryStateHarness(Path(tmp))
        _prepare_complete_recovery_state(host)

        if host.host_access_dir.exists():
            raise AssertionError(
                "Recovery-state больше не должен требовать каталог access/"
            )
        host.verify_recovery_state()


def test_recovery_preflight_rejects_partial_state() -> None:
    """Остановиться при неполных обязательных данных восстановления."""
    with tempfile.TemporaryDirectory() as tmp:
        host = RecoveryStateHarness(Path(tmp))
        _prepare_complete_recovery_state(host)
        host.host_openbao_unseal_key.unlink()
        try:
            host.verify_recovery_state()
        except BootstrapError as exc:
            if "unseal.key" not in str(exc):
                raise
        else:
            raise AssertionError("Recovery без unseal key должен быть запрещён")


class ManagedRecoveryStateHarness(RecoveryStateHarness):
    """Имитирует наличие управляемых объектов в PVE."""
    def managed_guests_exist(self) -> bool:
        """Вернуть испытательный признак существования управляемых гостей."""
        return True


def test_recovery_preflight_requires_opentofu_state_for_managed_pool() -> None:
    """Требовать состояние OpenTofu, если существуют управляемые гости."""
    with tempfile.TemporaryDirectory() as tmp:
        host = ManagedRecoveryStateHarness(Path(tmp))
        _prepare_complete_recovery_state(host)
        host.host_state_opentofu_file.unlink()
        try:
            host.verify_recovery_state()
        except BootstrapError as exc:
            if "OpenTofu state" not in str(exc):
                raise
        else:
            raise AssertionError(
                "Recovery managed-гостей без OpenTofu state должен быть запрещён"
            )


class MigrationGuardHarness(ApplyHarness):
    """Отвергает несовместимую старую схему каталогов."""
    def verify_persistent_layout(self) -> None:
        """Подменить проверку постоянных подключений."""
        self.events.append("verify_layout")
        raise BootstrapError(
            "автоматическая миграция существующих данных запрещена"
        )


def test_existing_layout_requires_manual_migration() -> None:
    """Отвергать старую схему подключений без автоматического переноса."""
    host = MigrationGuardHarness(
        "apply",
        infra_exists=True,
    )
    try:
        host.apply()
    except BootstrapError as exc:
        if "автоматическая миграция" not in str(exc):
            raise
    else:
        raise AssertionError(
            "Существующий 910 без новой схемы должен требовать ручную миграцию"
        )

    assert_equal(
        host.events,
        ["infra_exists", "verify_layout"],
        "До ручной миграции стандартный bootstrap не должен изменять 910",
    )


class ExecuteHarness(BootstrapHost):
    """Подменяет внешние команды точки входа."""
    def __init__(self, mode: str) -> None:
        """Подготовить изолированный испытательный контур вместо реального PVE."""
        super().__init__(mode)
        self.events: list[str] = []

    def require_host(self) -> None:
        """Подменить проверку root и средств PVE."""
        self.events.append("require_host")

    def verify_runner_contract(self) -> None:
        """Подменить проверку временного 990."""
        self.events.append("verify_runner")

    def execution_lock(self):
        """Подменить блокировку в испытании последовательности шагов."""
        return contextlib.nullcontext()

    def info(self, message: str) -> None:
        """Сохранить диагностическое событие для проверки порядка действий."""
        self.events.append("info")

    def verify_infra_ready(self, *, quiet: bool = False) -> None:
        """Записать запрос проверки работоспособности."""
        self.events.append("verify_ready:quiet" if quiet else "verify_ready")

    def finalize_runner(self) -> None:
        """Зарегистрировать успешное удаление временного 990."""
        self.events.append("finalize_runner")

    def check_ready(self) -> None:
        """Зафиксировать итоговую проверку отсутствия временного контура."""
        self.events.append("check_ready")


def test_check_mode_finishes_temporary_runner() -> None:
    """Проверка готовности должна завершить и удалить временный контур."""
    host = ExecuteHarness("check")
    host.execute()
    assert_equal(
        host.events,
        [
            "require_host",
            "verify_runner",
            "info",
            "verify_ready:quiet",
            "finalize_runner",
            "check_ready",
        ],
        "--check через публичный bootstrap должен удалить временный 990 после проверки 910",
    )


class ForeignRemoveHarness(BootstrapHost):
    """Защищает чужой гостевой объект от удаления."""
    def __init__(self) -> None:
        """Подготовить изолированный испытательный контур вместо реального PVE."""
        super().__init__("remove")
        self.events: list[str] = []

    def remove_runner_if_present(self) -> None:
        """Зарегистрировать очистку временного контейнера."""
        self.events.append("remove_runner")

    def infra_exists(self) -> bool:
        """Подменить проверку существования управляющего объекта."""
        return True

    def infra_config_is_expected(self) -> bool:
        """Вернуть ожидаемый результат проверки принадлежности."""
        return False

    def pct(self, *args: str, **kwargs):
        """Записать или запретить административную команду в испытании."""
        self.events.append("pct:" + " ".join(args))
        raise AssertionError("Разрушительная команда pct не должна выполняться для чужого 910")


def test_remove_rejects_foreign_910() -> None:
    """Не выполнять разрушительные команды для чужого объекта."""
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


def test_bootstrap_command_run_modes() -> None:
    """Перенос команд не меняет обычный и тихий режимы вывода."""
    with tempfile.TemporaryDirectory() as temp:
        host = BootstrapHost("check")
        host.log_file = Path(temp) / "bootstrap.log"
        result = host.run(
            sys.executable, "-c", "print('captured')",
            capture=True,
        )
        assert_equal(result.returncode, 0, "Обычная команда должна завершиться успешно")
        assert_equal(result.stdout.strip(), "captured", "Обычный вывод должен возвращаться")

        silent = host.run(
            sys.executable, "-c", "print('logged')",
            quiet=True,
        )
        assert_equal(silent.returncode, 0, "Тихая команда должна завершиться успешно")
        if "logged" not in host.log_file.read_text(encoding="utf-8"):
            raise AssertionError("Тихая команда должна записывать вывод в журнал")


def test_bootstrap_path_groups() -> None:
    """Пути состояния и доступа должны остаться прежними после выделения."""
    host = BootstrapHost("check")
    assert_equal(
        host.host_state_dir,
        Path("/mnt/bindmounts/infra-manager/state"),
        "Постоянные данные должны оставаться на PVE",
    )
    assert_equal(
        host.infra_state_dir,
        Path("/mnt/persistent-state"),
        "Управляющий контейнер должен видеть ту же точку подключения",
    )
    assert_equal(
        host.host_lock_file,
        Path("/run/proxmox-bootstrap-runner.lock"),
        "Блокировка должна находиться на PVE",
    )


def test_installation_marker_states() -> None:
    """Маркер различает чистую, прерванную, готовую и утраченную установку."""
    with tempfile.TemporaryDirectory() as temp:
        host = BootstrapHost("apply")
        host.host_persistent_root = Path(temp) / "state"
        host.host_pve_only_dir = host.host_persistent_root / "pve-only"
        assert_equal(host.installation_state(guest_exists=False), "new", "Чистый PVE")
        host.host_pve_only_dir.mkdir(parents=True)
        host.write_installation_marker("installing")
        assert_equal(host.installation_state(guest_exists=False), "unfinished", "Незаконченная установка")
        assert_equal(host.installation_state(guest_exists=True), "unfinished", "Неоконченная установка с LXC")
        host.write_installation_marker("ready")
        assert_equal(host.installation_state(guest_exists=True), "existing", "Готовый LXC")
        assert_equal(host.installation_state(guest_exists=False), "recovery", "Утраченный LXC")

        host._installation_marker_path().write_text(
            '{"schema_version":1,"role":"infra-manager","vmid":999,"state":"installing"}',
            encoding="utf-8",
        )
        try:
            host.installation_state(guest_exists=False)
        except BootstrapError:
            pass
        else:
            raise AssertionError("Чужой маркер установки должен отклоняться")

        host._installation_marker_path().unlink()
        assert_equal(host.installation_state(guest_exists=False), "recovery",
                     "Неопознанные сохранённые данные требуют recovery")


def test_exclusive_bootstrap_lock() -> None:
    """Второй экземпляр должен завершаться до изменения PVE."""
    with tempfile.TemporaryDirectory() as temp:
        first = BootstrapHost("check")
        second = BootstrapHost("apply")
        lock_path = Path(temp) / "bootstrap.lock"
        first.host_lock_file = lock_path
        second.host_lock_file = lock_path
        with first.execution_lock():
            try:
                with second.execution_lock():
                    raise AssertionError("Параллельная операция получила блокировку")
            except BootstrapError as exc:
                if "уже выполняется" not in str(exc):
                    raise
        with second.execution_lock():
            pass


class FinishCheckHarness(ExecuteHarness):
    """Подменяет маркер незавершённой установки."""
    def _read_installation_marker(self) -> str | None:
        """Вернуть испытательное состояние маркера."""
        return "installing"

    def write_installation_marker(self, state: str) -> None:
        """Сохранить состояние маркера в памяти испытания."""
        assert state == "ready"
        self.events.append("mark_ready")


def test_check_marks_verified_installation_ready() -> None:
    """Записать ready только после проверки готовности."""
    host = FinishCheckHarness("check")
    host.execute()
    assert_equal(
        host.events,
        ["require_host", "verify_runner", "info", "verify_ready:quiet",
         "mark_ready", "finalize_runner", "check_ready"],
        "Проверка после прерывания должна фиксировать подтверждённую готовность",
    )


class FailingExecuteHarness(ExecuteHarness):
    """Имитирует ошибку развёртывания перед отзывом токена."""
    def apply(self) -> None:
        """Смоделировать ошибку основного процесса."""
        self.events.append("apply")
        raise BootstrapError("Прерванный bootstrap")

    def remove_private_access(self) -> None:
        """Зарегистрировать отзыв временного токена."""
        self.events.append("revoke_token")


def test_token_revoked_on_bootstrap_failure() -> None:
    """Отозвать временный токен, сохранив исходную ошибку установки."""
    host = FailingExecuteHarness("apply")
    try:
        host.execute()
    except BootstrapError:
        pass
    else:
        raise AssertionError("Исключение первоначальной установки должно дойти до вызывающего кода")
    assert_equal(
        host.events,
        ["require_host", "verify_runner", "info", "apply", "revoke_token"],
        "При ошибке временный токен нужно отозвать без удаления 990",
    )


def test_invalid_installation_markers_fail_closed() -> None:
    """Повреждённый маркер или ссылка не должны разрешать повторную установку."""
    with tempfile.TemporaryDirectory() as temp:
        host = BootstrapHost("apply")
        host.host_persistent_root = Path(temp) / "persistent"
        host.host_pve_only_dir = host.host_persistent_root / "pve-only"
        host.host_pve_only_dir.mkdir(parents=True)
        marker = host._installation_marker_path()
        for invalid in ("{", '{"schema_version":1,"state":"bad"}'):
            marker.write_text(invalid, encoding="utf-8")
            try:
                host.installation_state(guest_exists=False)
            except BootstrapError:
                pass
            else:
                raise AssertionError("Неверный маркер не должен выбирать новую установку")
        marker.unlink()
        target = Path(temp) / "unexpected.json"
        target.write_text("{}", encoding="utf-8")
        marker.symlink_to(target)
        try:
            host.installation_state(guest_exists=False)
        except BootstrapError as exc:
            if "символьная ссылка" not in str(exc):
                raise
        else:
            raise AssertionError("Маркер-ссылка должен блокировать восстановление")


def test_unfinished_installation_rejects_unseal_key() -> None:
    """Наличие ключа OpenBao запрещает автоматическое пересоздание rootfs."""
    with tempfile.TemporaryDirectory() as temp:
        host = BootstrapHost("apply")
        root = Path(temp) / "persistent"
        host.host_persistent_root = root
        host.host_pve_only_dir = root / "pve-only"
        host.host_state_dir = root / "state"
        host.host_openbao_unseal_key = root / "pve-only/openbao/unseal.key"
        host.host_pve_only_dir.mkdir(parents=True)
        host.host_state_dir.mkdir()
        host.write_installation_marker("installing")
        host.host_openbao_unseal_key.parent.mkdir()
        host.host_openbao_unseal_key.write_text("test-secret", encoding="utf-8")
        try:
            host.verify_unfinished_installation(guest_exists=False)
        except BootstrapError as exc:
            if "постоянные данные" not in str(exc):
                raise
        else:
            raise AssertionError("Начатое состояние OpenBao должно блокировать повтор")


class FailingCheckHarness(ExecuteHarness):
    """Имитирует отказ проверки готовности до очистки временного контейнера."""

    def verify_infra_ready(self, *, quiet: bool = False) -> None:
        """Остановить проверку с диагностической ошибкой."""
        self.events.append("verify_failed")
        raise BootstrapError("Управляющий контур не готов")

    def remove_private_access(self) -> None:
        """Зарегистрировать обязательный отзыв временного токена."""
        self.events.append("revoke_token")


def test_check_failure_revokes_token_without_destroying_runner() -> None:
    """Ошибка --check не должна удалять 990, но должна отзывать токен."""
    host = FailingCheckHarness("check")
    try:
        host.execute()
    except BootstrapError as exc:
        if "не готов" not in str(exc):
            raise
    else:
        raise AssertionError("Ошибка проверки должна дойти до вызывающего кода")
    assert_equal(
        host.events,
        ["require_host", "verify_runner", "info", "verify_failed", "revoke_token"],
        "После неудачного --check токен удаляется, а временный 990 сохраняется",
    )


class FailingTokenRevokeHarness(FailingExecuteHarness):
    """Имитирует ошибку отзыва токена во время обработки первого отказа."""

    def remove_private_access(self) -> None:
        """Сообщить об ошибке отдельной операции очистки."""
        self.events.append("revoke_failed")
        raise BootstrapError("токен остался действующим")


def test_failed_token_revoke_preserves_original_error() -> None:
    """Вторичная ошибка очистки не должна скрывать причину первого отказа."""
    host = FailingTokenRevokeHarness("apply")
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        try:
            host.execute()
        except BootstrapError as exc:
            if "Прерванный bootstrap" not in str(exc):
                raise
        else:
            raise AssertionError("Должна сохраниться ошибка основного этапа")
    if "токен PVE" not in stderr.getvalue() or "токен остался" not in stderr.getvalue():
        raise AssertionError("Оператор должен увидеть отдельную ошибку отзыва токена")
    if "revoke_failed" not in host.events:
        raise AssertionError("Отзыв должен быть выполнен даже после ошибки bootstrap")


class DirtyFinalizationHarness(BootstrapHost):
    """Отказывает в финализации, если после удаления остались секреты 990."""

    def __init__(self) -> None:
        """Подготовить проверку без настоящих команд PVE."""
        super().__init__("check")
        self.events: list[str] = []

    def assert_owned_runner(self) -> None:
        """Считать принадлежность временного 990 ранее проверенной."""
        self.events.append("owned")

    def remove_bootstrap_ssh_access_from_infra(self) -> None:
        """Зарегистрировать попытку удалить временное разрешение."""
        self.events.append("remove_bootstrap_ssh")

    def ct_exec(self, *args: str, **kwargs):
        """Имитировать сохранившиеся файлы после команды rm."""
        self.events.append("ct:" + str(args[0]))
        return SimpleNamespace(returncode=1 if args[0] == "test" else 0)

    def pct(self, *args: str, **kwargs):
        """Запретить остановку/удаление 990 до очистки секретов."""
        raise AssertionError("Нельзя удалять 990, пока временные секреты остались")


def test_cleanup_stops_if_temporary_secrets_remain() -> None:
    """Очистка останавливается до pct destroy при остающихся файлах."""
    host = DirtyFinalizationHarness()
    try:
        host.finalize_runner()
    except BootstrapError as exc:
        if "временные данные" not in str(exc):
            raise
    else:
        raise AssertionError("Неочищенные секреты должны блокировать завершение")
    assert_equal(
        host.events,
        ["owned", "remove_bootstrap_ssh", "ct:rm", "ct:test"],
        "Должна проверяться очистка ещё до обращения к pct destroy",
    )


def test_lock_rejects_symlink_without_touching_target() -> None:
    """Символьная ссылка вместо блокировки не должна изменять чужой файл."""
    with tempfile.TemporaryDirectory() as temp:
        host = BootstrapHost("apply")
        target = Path(temp) / "data"
        target.write_text("preserve", encoding="utf-8")
        host.host_lock_file = Path(temp) / "lock"
        host.host_lock_file.symlink_to(target)
        try:
            with host.execution_lock():
                raise AssertionError("Нельзя получить блокировку через ссылку")
        except BootstrapError as exc:
            if "блокировки" not in str(exc):
                raise
        assert_equal(target.read_text(encoding="utf-8"), "preserve",
                     "Чужой файл нельзя изменять")


class NonDestructiveRemoveHarness(ExecuteHarness):
    """Подменяет удаление управляющего гостя без реального обращения к PVE."""

    def remove_infra(self) -> None:
        """Запомнить вызов удаления без изменения инфраструктуры."""
        self.events.append("remove_infra")


def test_purge_keeps_persistent_storage() -> None:
    """Режим purge удаляет каталог загрузчика, сохраняя данные PVE."""
    with tempfile.TemporaryDirectory() as temp:
        host = NonDestructiveRemoveHarness("purge")
        host.host_bootstrap_dir = Path(temp) / "bootstrap"
        host.host_persistent_root = Path(temp) / "permanent"
        host.host_bootstrap_dir.mkdir()
        host.host_persistent_root.mkdir()
        (host.host_persistent_root / "openbao.db").write_text(
            "data", encoding="utf-8"
        )
        host.execute()
        if host.host_bootstrap_dir.exists():
            raise AssertionError("Каталог старого загрузчика должен быть удалён")
        if not (host.host_persistent_root / "openbao.db").is_file():
            raise AssertionError("Режим purge не должен уничтожать постоянные данные")
        if "remove_infra" not in host.events:
            raise AssertionError("Режим purge должен выполнить операцию удаления")


def test_bootstrap_python_sources_follow_rule_010() -> None:
    """У каждого Python-модуля, класса и метода должна быть строка документации."""
    program = ROOT / "scripts/bootstrap-runner"
    checked = sorted(program.rglob("*.py"))
    checked.append(Path(__file__))
    for path in checked:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        if not ast.get_docstring(tree):
            raise AssertionError(f"Модуль без описания по правилу 010: {path}")
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if not ast.get_docstring(node):
                    raise AssertionError(
                        f"Отсутствует описание по правилу 010: "
                        f"{path}:{node.lineno} {node.name}"
                    )


def main() -> None:
    """Последовательно выполнить все проверки без доступа к рабочему PVE."""
    tests = [
        test_infra_manager_role_can_move_to_another_vmid,
        test_role_is_read_from_project_inside_runner,
        test_role_lookup_is_identical_in_runner_and_host,
        test_strict_ownership_marker,
        test_runner_ownership_requires_exact_markers,
        test_full_pve_token_contract,
        test_full_pve_token_missing_secret_is_removed,
        test_full_pve_token_invalid_json_is_removed,
        test_pve_node_address,
        test_bootstrap_timing_success_and_failure,
        test_section_totals_include_interrupted_section,
        test_progress_streaming,
        test_bootstrap_steps_enable_progress,
        test_infra_ready_uses_status_as_final_screen,
        test_attach_persistent_layout_temporarily_disables_protection,
        test_attach_persistent_layout_restores_protection_on_failure,
        test_recovery_rootfs_removal_is_strict,
        test_control_plane_step_depends_on_bootstrap_mode,
        test_new_install_flow,
        test_auto_recovery_for_absent_infra_and_existing_data,
        test_auto_recovery_stops_on_incomplete_data,
        test_existing_infra_with_data_is_not_automatically_recreated,
        test_existing_infra_requires_normal_operations,
        test_recovery_recreates_existing_infra,
        test_recovery_runner_failure_keeps_existing_910,
        test_recovery_recreates_missing_infra,
        test_recovery_preflight_accepts_complete_state,
        test_recovery_preflight_allows_missing_approle_files,
        test_recovery_preflight_allows_missing_access_directory,
        test_recovery_preflight_rejects_partial_state,
        test_recovery_preflight_requires_opentofu_state_for_managed_pool,
        test_existing_layout_requires_manual_migration,
        test_check_mode_finishes_temporary_runner,
        test_remove_rejects_foreign_910,
        test_installation_marker_states,
        test_bootstrap_path_groups,
        test_bootstrap_command_run_modes,
        test_restart_unfinished_installation,
        test_restart_rejects_persistent_data,
        test_token_revoked_on_bootstrap_failure,
        test_exclusive_bootstrap_lock,
        test_check_marks_verified_installation_ready,
        test_invalid_installation_markers_fail_closed,
        test_unfinished_installation_rejects_unseal_key,
        test_check_failure_revokes_token_without_destroying_runner,
        test_failed_token_revoke_preserves_original_error,
        test_cleanup_stops_if_temporary_secrets_remain,
        test_lock_rejects_symlink_without_touching_target,
        test_purge_keeps_persistent_storage,
        test_bootstrap_python_sources_follow_rule_010,
    ]
    for test in tests:
        test()
    print(f"[ОК] Проверки bootstrap-host.py пройдены: {len(tests)}")


if __name__ == "__main__":
    main()
