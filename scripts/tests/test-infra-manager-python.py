#!/usr/bin/env python3
"""Минимальные unit/contract checks Python-части infra-manager."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = ROOT / "scripts" / "infra-manager"
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager import guest_deploy as guest_deploy_module  # noqa: E402
from infra_manager import status as status_module  # noqa: E402
from infra_manager.cli import main  # noqa: E402
from infra_manager.common import (  # noqa: E402
    CommandError,
    CommandRunner,
    InfraManagerError,
    _redact_argv,
    run,
)
from infra_manager.guest_deploy import (  # noqa: E402
    DeploymentContext,
    DeploymentPaths,
    _apply_plan,
    _build_guest_plan,
    _find_guest_directory,
    _reconcile_guest_infrastructure,
    _validate_pve_and_state,
)
from infra_manager.host_setup import (  # noqa: E402
    BASE_PACKAGES,
    PVE_API_ENV,
    HostSetup,
)
from infra_manager.opentofu import OpenTofuWorkspace  # noqa: E402
from infra_manager.pve import (  # noqa: E402
    PveClient,
    permission_present,
    select_management_ipv4,
)
from infra_manager.runtime_setup import (  # noqa: E402
    PACKER_VERSION,
    RuntimeSetup,
)
from infra_manager.semaphore import (  # noqa: E402
    PROJECT_REPO,
    SEMAPHORE_TEMPLATES,
    find_unique_by_name,
    require_unique_by_name,
)
from infra_manager.settings import PATHS, SETTINGS  # noqa: E402
from infra_manager.setup import Setup  # noqa: E402
from infra_manager.status import (  # noqa: E402
    SemaphoreSnapshot,
    _check_git_branch_contract,
    _project_branch,
    load_semaphore_snapshot,
    validate_semaphore_snapshot,
)
from infra_manager.template import (  # noqa: E402
    _packer_inputs,
    _template_failures,
    _validate_cloud_status,
)


def fail(message: str) -> None:
    raise SystemExit(message)


def main_test() -> None:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        rc = main([])
    if rc != 0 or "infra-manager" not in stdout.getvalue():
        fail("infra-manager CLI без аргументов не вывел справку")

    result = run(
        [sys.executable, "-c", "print('OK')"],
        capture_output=True,
    )
    if result.stdout.strip() != "OK":
        fail("common.run не вернул stdout внешней команды")

    try:
        run(
            [sys.executable, "-c", "import sys; sys.exit(7)"],
            capture_output=True,
        )
    except CommandError as exc:
        if exc.returncode != 7:
            fail(
                f"CommandError содержит неверный returncode: "
                f"{exc.returncode}"
            )
    else:
        fail("common.run не сообщил об ошибке внешней команды")

    runner = CommandRunner()
    captured = runner.run(
        [sys.executable, "-c", "print('CAPTURED')"],
        capture=True,
    )
    if captured.stdout.strip() != "CAPTURED":
        fail("CommandRunner capture не вернул stdout")

    with tempfile.TemporaryDirectory() as tmp:
        command_log = Path(tmp) / "command.log"
        logged_runner = CommandRunner(default_log_file=command_log)
        logged_runner.run(
            [
                sys.executable,
                "-c",
                "print('LOGGED')",
                "--api-key",
                "secret-value",
            ],
            sensitive_args=(4,),
        )
        log_text = command_log.read_text(encoding="utf-8")
        if "LOGGED" not in log_text:
            fail("CommandRunner не записал вывод команды в лог")
        if "secret-value" in log_text:
            fail("CommandRunner записал чувствительный аргумент в лог")

    redacted = _redact_argv(
        [
            "tool",
            "--token",
            "abc",
            "--password=def",
            "-var=proxmox_token=ghi",
            "-var=other=value",
        ]
    )
    if redacted != (
        "tool",
        "--token",
        "[СКРЫТО]",
        "--password=[СКРЫТО]",
        "-var=proxmox_token=[СКРЫТО]",
        "-var=other=value",
    ):
        fail(f"Некорректное редактирование чувствительных аргументов: {redacted!r}")

    nested = _redact_argv(
        ["tool", "-var=proxmox_token=abc=def", "next"]
    )
    if nested != (
        "tool",
        "-var=proxmox_token=[СКРЫТО]",
        "next",
    ):
        fail(f"Вложенный '=' обработан неверно: {nested!r}")

    explicit = _redact_argv(
        ["tool", "--api-key", "secret-value", "next"],
        sensitive_indices=(2,),
    )
    if explicit != ("tool", "--api-key", "[СКРЫТО]", "next"):
        fail(f"sensitive_indices обработан неверно: {explicit!r}")

    packer_client = SimpleNamespace(
        url="https://pve.example:8006",
        token_id="root@pam!infra-manager",
        token_secret="pve-secret",
    )
    packer_env, packer_args = _packer_inputs(
        {"BASE": "1"},
        client=packer_client,
        node="pve",
        iso_url="https://example.invalid/debian.iso",
        iso_checksum="sha512:deadbeef",
        build_password="build-secret",
    )
    if packer_env.get("PKR_VAR_proxmox_token") != "pve-secret":
        fail("Packer API token не передан через PKR_VAR_proxmox_token")
    if packer_env.get("PKR_VAR_build_password") != "build-secret":
        fail("Packer build password не передан через PKR_VAR_build_password")
    joined_packer_args = " ".join(packer_args)
    if "pve-secret" in joined_packer_args or "build-secret" in joined_packer_args:
        fail("Секрет Packer попал в аргументы командной строки")
    if any(
        arg.startswith("-var=proxmox_token=")
        or arg.startswith("-var=build_password=")
        for arg in packer_args
    ):
        fail("Секретные переменные Packer всё ещё передаются через -var")

    workspace = OpenTofuWorkspace(
        directory=Path("/tmp/opentofu"),
        state_dir=Path("/tmp/state"),
        env={"SSL_CERT_FILE": "/tmp/ca.crt"},
        payload={"guests": {}},
    )
    deployment_paths = DeploymentPaths(
        guest_dir=Path("/tmp/guest"),
        private_key=Path("/tmp/key"),
        playbook=Path("/tmp/playbook.yml"),
        known_hosts=Path("/tmp/known_hosts"),
        plan_file=Path("/tmp/410.tfplan"),
    )

    def deployment_for(client: object, *, paths: DeploymentPaths = deployment_paths):
        return DeploymentContext(
            client=client,
            vmid=410,
            name="test-vm",
            node="pve",
            template_vmid=9000,
            address="192.0.2.10",
            target='proxmox_virtual_environment_vm.guest["410"]',
            workspace=workspace,
            paths=paths,
        )

    deployment_client = SimpleNamespace(
        find_vm=lambda vmid: {"type": "qemu", "name": "test-vm"}
    )
    deployment = deployment_for(deployment_client)
    if (
        _validate_pve_and_state(
            deployment,
            state_present=True,
            state_status="ready",
        )
        is not None
    ):
        fail("Проверка состояния VM должна возвращать None")

    invalid_state_cases = (
        (
            SimpleNamespace(
                find_vm=lambda vmid: {"type": "lxc", "name": "test-vm"}
            ),
            True,
            "ready",
            "VMID, занятый LXC",
        ),
        (deployment_client, False, "", "VM без OpenTofu state"),
        (SimpleNamespace(find_vm=lambda vmid: None), True, "ready", "state без VM"),
    )
    for client, state_present, state_status, description in invalid_state_cases:
        try:
            _validate_pve_and_state(
                deployment_for(client),
                state_present=state_present,
                state_status=state_status,
            )
        except InfraManagerError:
            pass
        else:
            fail(f"Проверка состояния приняла опасный случай: {description}")

    class TaintedClient:
        def __init__(self) -> None:
            self.resource = {"type": "qemu", "name": "test-vm"}
            self.tasks: list[tuple[str, str]] = []

        def find_vm(self, vmid: int):
            return self.resource

        def vm_status(self, *, node: str, vmid: int) -> str:
            return "stopped"

        def put(self, path: str, *, form: dict[str, int]) -> None:
            self.tasks.append(("PUT", path))

        def run_task(self, method: str, path: str, **kwargs: object) -> None:
            self.tasks.append((method, path))
            if method == "DELETE":
                self.resource = None

    tainted_client = TaintedClient()
    tainted_commands: list[list[str]] = []

    def record_tainted_run(argv: list[str], **kwargs: object):
        tainted_commands.append(argv)
        return SimpleNamespace(returncode=0, stdout="")

    with patch.object(guest_deploy_module, "run", record_tainted_run):
        _validate_pve_and_state(
            deployment_for(tainted_client),
            state_present=True,
            state_status="tainted",
        )
    if not any(method == "DELETE" for method, _ in tainted_client.tasks):
        fail("Tainted VM не удалена после проверки типа и имени")
    if not any(command[2:4] == ["state", "rm"] for command in tainted_commands):
        fail("Tainted VM не удалена из OpenTofu state")

    def unexpected_plan_run(argv: list[str], **kwargs: object):
        if "-json" in argv:
            payload = {
                "resource_changes": [
                    {
                        "address": "proxmox_virtual_environment_vm.guest[\"999\"]",
                        "change": {"actions": ["create"]},
                    }
                ]
            }
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload))
        return SimpleNamespace(returncode=0, stdout="")

    with patch.object(guest_deploy_module, "run", unexpected_plan_run):
        try:
            _build_guest_plan(deployment)
        except InfraManagerError:
            pass
        else:
            fail("Target plan разрешил изменение постороннего ресурса")

    def target_plan_run(argv: list[str], **kwargs: object):
        if "-json" in argv:
            payload = {
                "resource_changes": [
                    {
                        "address": deployment.target,
                        "change": {"actions": ["update"]},
                    }
                ]
            }
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload))
        return SimpleNamespace(returncode=0, stdout="")

    with patch.object(guest_deploy_module, "run", target_plan_run):
        guest_plan = _build_guest_plan(deployment)
    if guest_plan.actions != ("update",):
        fail("Target plan неверно разобрал действия выбранной VM")

    class ProtectedTemplateClient:
        def __init__(self) -> None:
            self.protection_values: list[int] = []

        def vm_config(self, *, node: str, vmid: int) -> dict[str, int]:
            return {"template": 1, "protection": 1}

        def put(self, path: str, *, form: dict[str, int]) -> None:
            self.protection_values.append(form["protection"])

    protected_client = ProtectedTemplateClient()

    def failed_apply(argv: list[str], **kwargs: object):
        raise InfraManagerError("expected apply failure")

    with patch.object(guest_deploy_module, "run", failed_apply):
        try:
            _apply_plan(deployment_for(protected_client), ["create"])
        except InfraManagerError:
            pass
        else:
            fail("Тест ожидал ошибку tofu apply")
    if protected_client.protection_values != [0, 1]:
        fail("Защита шаблона не восстановлена после ошибки tofu apply")

    with tempfile.TemporaryDirectory() as tmp:
        plan_file = Path(tmp) / "410.tfplan"
        plan_file.touch()
        temporary_paths = DeploymentPaths(
            guest_dir=deployment_paths.guest_dir,
            private_key=deployment_paths.private_key,
            playbook=deployment_paths.playbook,
            known_hosts=deployment_paths.known_hosts,
            plan_file=plan_file,
        )
        with patch.object(
            guest_deploy_module,
            "_build_guest_plan",
            side_effect=InfraManagerError("expected plan failure"),
        ):
            try:
                _reconcile_guest_infrastructure(
                    deployment_for(deployment_client, paths=temporary_paths)
                )
            except InfraManagerError:
                pass
            else:
                fail("Тест ожидал ошибку построения OpenTofu plan")
        if plan_file.exists():
            fail("Временный OpenTofu plan не удалён после ошибки")

    previous_branch = os.environ.get("INFRA_PROJECT_BRANCH")
    try:
        os.environ["INFRA_PROJECT_BRANCH"] = "feature/test-branch"
        if _project_branch() != "feature/test-branch":
            fail("status не учитывает INFRA_PROJECT_BRANCH")
    finally:
        if previous_branch is None:
            os.environ.pop("INFRA_PROJECT_BRANCH", None)
        else:
            os.environ["INFRA_PROJECT_BRANCH"] = previous_branch

    branch = "feature/test-branch"
    repository = {
        "git_url": PROJECT_REPO,
        "ssh_key_id": 7,
        "git_branch": branch,
    }
    template_names = [spec.name for spec in SEMAPHORE_TEMPLATES]
    if len(template_names) != len(set(template_names)):
        fail("SEMAPHORE_TEMPLATES содержит повторяющиеся имена")
    for spec in SEMAPHORE_TEMPLATES:
        if spec.app != "python":
            fail(f"Шаблон Semaphore '{spec.name}' использует неожиданный app")
        if not (ROOT / spec.playbook).is_file():
            fail(f"Не найден сценарий Semaphore: {spec.playbook}")

    templates = [
        {"name": spec.name, "git_branch": branch}
        for spec in SEMAPHORE_TEMPLATES
    ]
    _check_git_branch_contract(
        repository,
        ["main", branch],
        templates,
        github_key_id=7,
        project_branch=branch,
    )

    wrong_repository = dict(repository)
    wrong_repository["git_branch"] = "main"
    try:
        _check_git_branch_contract(
            wrong_repository,
            ["main", branch],
            templates,
            github_key_id=7,
            project_branch=branch,
        )
    except InfraManagerError:
        pass
    else:
        fail("status принял repository.git_branch другой ветки")

    wrong_templates = [dict(item) for item in templates]
    wrong_templates[1]["git_branch"] = "main"
    try:
        _check_git_branch_contract(
            repository,
            ["main", branch],
            wrong_templates,
            github_key_id=7,
            project_branch=branch,
        )
    except InfraManagerError:
        pass
    else:
        fail("status принял template.git_branch другой ветки")

    cli_help_cases = (
        ["--help"],
        ["setup", "--help"],
        ["semaphore-project", "--help"],
        ["status", "--help"],
        ["pve-access-check", "--help"],
    )
    for args in cli_help_cases:
        direct = subprocess.run(
            [sys.executable, "-m", "infra_manager", *args],
            cwd=MODULE_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if direct.returncode != 0:
            fail(
                f"python -m infra_manager {' '.join(args)} "
                f"вернул {direct.returncode}"
            )
        if "usage: infra-manager" not in direct.stdout:
            fail(
                f"python -m infra_manager {' '.join(args)} "
                "не вывел usage"
            )

    if "python3" not in BASE_PACKAGES:
        fail("python3 отсутствует в контракте setup")
    if "python3-yaml" not in BASE_PACKAGES:
        fail("python3-yaml отсутствует в контракте setup")

    package_dir = MODULE_ROOT / "infra_manager"
    for module_path in package_dir.glob("*.py"):
        if module_path.name == "common.py":
            continue
        source = module_path.read_text(encoding="utf-8")
        if "subprocess.run(" in source:
            fail(
                "Внешние команды должны выполняться только через "
                f"CommandRunner: {module_path.name}"
            )

    if PATHS.pve_api_env != Path("/etc/infra-manager/secrets/pve-api.env"):
        fail("Единый путь PVE credential имеет неожиданное значение")
    if PATHS.ca_bundle != Path("/etc/infra-manager/ca/ca-bundle.crt"):
        fail("Единый путь CA bundle имеет неожиданное значение")
    if PATHS.opentofu_input != Path("/var/lib/infra-manager/opentofu/guests.json"):
        fail("Единый путь OpenTofu input имеет неожиданное значение")
    if SETTINGS.default_project_branch != "main":
        fail("Ветка проекта по умолчанию должна быть main")
    if PACKER_VERSION != SETTINGS.packer_version:
        fail("setup использует версию Packer не из единых настроек")
    if PVE_API_ENV != PATHS.pve_api_env:
        fail("setup использует путь PVE credential не из единых настроек")

    compose = Setup.compose("ps")
    if compose[:2] != ["docker", "compose"] or compose[-1] != "ps":
        fail(f"Некорректная команда Docker Compose: {compose!r}")
    if not issubclass(Setup, HostSetup) or not issubclass(Setup, RuntimeSetup):
        fail("Setup не объединяет этапы подготовки хоста и runtime")

    one = find_unique_by_name(
        [{"id": 1, "name": "proxmox"}],
        "proxmox",
        "Git repository",
    )
    if one is None or one.get("id") != 1:
        fail("find_unique_by_name не вернул единственный объект")

    none = find_unique_by_name(
        [{"id": 1, "name": "other"}],
        "proxmox",
        "Git repository",
    )
    if none is not None:
        fail("find_unique_by_name не вернул None для отсутствующего объекта")

    try:
        find_unique_by_name(
            [
                {"id": 1, "name": "proxmox"},
                {"id": 2, "name": "proxmox"},
            ],
            "proxmox",
            "Git repository",
        )
    except InfraManagerError:
        pass
    else:
        fail("find_unique_by_name разрешил дубликаты")

    try:
        require_unique_by_name([], "proxmox", "Git repository")
    except InfraManagerError:
        pass
    else:
        fail("require_unique_by_name принял отсутствующий объект")

    semaphore_snapshot = SemaphoreSnapshot(
        project_id=1,
        github_key={
            "id": 7,
            "name": "GitHub project read-only",
            "type": "ssh",
        },
        repository=repository,
        branches=("main", branch),
        environments=(
            {"id": 8, "name": SETTINGS.opentofu_env_name},
        ),
        templates=tuple(
            {
                "name": spec.name,
                "git_branch": branch,
                "app": spec.app,
                "playbook": spec.playbook,
                "arguments": spec.arguments,
            }
            for spec in SEMAPHORE_TEMPLATES
        ),
    )
    validate_semaphore_snapshot(
        semaphore_snapshot,
        project_branch=branch,
    )

    class SnapshotClient:
        def __init__(self) -> None:
            self.paths: list[str] = []

        def get(self, path: str):
            self.paths.append(path)
            if "/keys?" in path:
                return [semaphore_snapshot.github_key]
            if "/repositories?" in path:
                return [{"id": 9, "name": "proxmox", **repository}]
            if "/environment?" in path:
                return list(semaphore_snapshot.environments)
            if "/templates?" in path:
                return list(semaphore_snapshot.templates)
            raise AssertionError(f"Неожиданный Semaphore endpoint: {path}")

    snapshot_client = SnapshotClient()
    with patch.object(
        status_module,
        "_load_repository_branches",
        return_value=["main", branch],
    ):
        loaded_snapshot = load_semaphore_snapshot(snapshot_client, 1)
    if loaded_snapshot != SemaphoreSnapshot(
        project_id=1,
        github_key=semaphore_snapshot.github_key,
        repository={"id": 9, "name": "proxmox", **repository},
        branches=("main", branch),
        environments=semaphore_snapshot.environments,
        templates=semaphore_snapshot.templates,
    ):
        fail("load_semaphore_snapshot неверно собрал состояние проекта")
    if len(snapshot_client.paths) != 4:
        fail("Semaphore snapshot должен читать каждый список ровно один раз")

    permissions = {
        "/vms": {
            "VM.Audit": 1,
            "VM.Allocate": True,
        }
    }
    if not permission_present(permissions, "VM.Audit"):
        fail("PVE permission_present не увидел числовое privilege")
    if not permission_present(permissions, "VM.Allocate"):
        fail("PVE permission_present не увидел boolean privilege")
    if permission_present(permissions, "Permissions.Modify"):
        fail("PVE permission_present нашёл отсутствующее privilege")

    vm_interfaces = {
        "result": [
            {
                "name": "lo",
                "ip-addresses": [
                    {"ip-address-type": "ipv4", "ip-address": "127.0.0.1"},
                ],
            },
            {
                "name": "eth0",
                "ip-addresses": [
                    {"ip-address-type": "ipv4", "ip-address": "192.168.1.77"},
                    {"ip-address-type": "ipv6", "ip-address": "fe80::1"},
                ],
            },
            {
                "name": "docker0",
                "ip-addresses": [
                    {"ip-address-type": "ipv4", "ip-address": "172.17.0.1"},
                ],
            },
        ],
    }
    selected = select_management_ipv4(vm_interfaces, "192.168.0.0/16")
    if str(selected) != "192.168.1.77":
        fail(f"Неверно выбран IPv4 VM: {selected}")

    lxc_interfaces = [
        {
            "name": "eth0",
            "ip-addresses": [
                {"ip-address-type": "inet", "ip-address": "192.168.2.40"},
            ],
        },
    ]
    selected = select_management_ipv4(lxc_interfaces, "192.168.0.0/16")
    if str(selected) != "192.168.2.40":
        fail(f"Неверно выбран IPv4 LXC: {selected}")

    if select_management_ipv4(vm_interfaces, "10.0.0.0/8") is not None:
        fail("Поиск DHCP-адреса нашёл IPv4 вне административной подсети")

    ambiguous = {
        "result": [
            {
                "ip-addresses": [
                    {"ip-address-type": "ipv4", "ip-address": "192.168.1.10"},
                    {"ip-address-type": "ipv4", "ip-address": "192.168.1.11"},
                ],
            },
        ],
    }
    try:
        select_management_ipv4(ambiguous, "192.168.0.0/16")
    except InfraManagerError:
        pass
    else:
        fail("Неоднозначный административный IPv4 не вызвал ошибку")

    calls: list[str] = []
    client = object.__new__(PveClient)
    client.data = lambda endpoint, **kwargs: calls.append(endpoint) or []
    client.guest_interfaces(node="pve", vmid=410, kind="vm")
    if calls[-1] != "/nodes/pve/qemu/410/agent/network-get-interfaces":
        fail(f"Неверный PVE endpoint VM: {calls[-1]}")
    client.guest_interfaces(node="pve", vmid=311, kind="lxc")
    if calls[-1] != "/nodes/pve/lxc/311/interfaces":
        fail(f"Неверный PVE endpoint LXC: {calls[-1]}")

    guest_dir = _find_guest_directory(ROOT, 410)
    if guest_dir.name != "410-ai-control":
        fail(f"Неверно найден каталог VM 410: {guest_dir}")

    valid_template = {
        "template": 1,
        "name": "tpl-debian13",
        "description": "template-version=8",
        "protection": 1,
        "scsihw": "virtio-scsi-single",
        "agent": "1",
        "ide0": "local-lvm:cloudinit",
        "ciuser": "root",
        "ipconfig0": "ip=dhcp",
        "ciupgrade": 0,
    }
    if _template_failures(valid_template):
        fail("Корректный шаблон 9000 не прошёл Python-проверку")

    invalid_template = dict(valid_template)
    invalid_template["ide0"] = ""
    if "ide0=<cloudinit отсутствует>" not in _template_failures(
        invalid_template
    ):
        fail("Python-проверка шаблона не обнаружила отсутствие Cloud-Init")

    _validate_cloud_status(
        '{"status":"done","init":{},"init-local":{},'
        '"modules-config":{},"modules-final":{}}'
    )

    print("infra-manager Python checks passed.")


if __name__ == "__main__":
    main_test()
