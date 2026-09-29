#!/usr/bin/env python3
"""Модульные и контрактные проверки развёртывания гостевых систем infra-manager."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = ROOT / "scripts" / "infra-manager"
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager import guest_deploy as guest_deploy_module
from infra_manager import opentofu as opentofu_module
from infra_manager.common import InfraManagerError
from infra_manager.guest_deploy import (
    DeploymentContext,
    DeploymentPaths,
    _apply_plan,
    _build_guest_plan,
    _find_guest_directory,
    _reconcile_guest_infrastructure,
    _show_guest_summary,
    _validate_pve_and_state,
)
from infra_manager.opentofu import OpenTofuWorkspace


def fail(message: str) -> None:
    raise SystemExit(message)


def check_opentofu_state_status() -> None:
    target = 'proxmox_virtual_environment_vm.guest["410"]'
    directory = Path("/tmp/opentofu")
    env = {"SSL_CERT_FILE": "/tmp/ca.crt"}

    with tempfile.TemporaryDirectory() as tmp:
        state_file = Path(tmp) / "proxmox.tfstate"

        with (
            patch.object(opentofu_module, "STATE_FILE", state_file),
            patch.object(opentofu_module, "run") as mocked_run,
        ):
            if opentofu_module._state_status(directory, target, env) != (
                False,
                "",
            ):
                fail("Отсутствующий файл состояния должен означать отсутствие ресурса")
            mocked_run.assert_not_called()

        state_file.write_text("{}\n", encoding="utf-8")
        state_payload = {
            "resources": [
                {
                    "type": "proxmox_virtual_environment_vm",
                    "name": "guest",
                    "instances": [
                        {
                            "index_key": "410",
                            "status": "tainted",
                        }
                    ],
                }
            ]
        }
        with (
            patch.object(opentofu_module, "STATE_FILE", state_file),
            patch.object(
                opentofu_module,
                "run",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(state_payload),
                ),
            ),
        ):
            if opentofu_module._state_status(directory, target, env) != (
                True,
                "tainted",
            ):
                fail("Состояние OpenTofu неверно определило найденный ресурс")

        missing_payload = {"resources": []}
        with (
            patch.object(opentofu_module, "STATE_FILE", state_file),
            patch.object(
                opentofu_module,
                "run",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(missing_payload),
                ),
            ),
        ):
            if opentofu_module._state_status(directory, target, env) != (
                False,
                "",
            ):
                fail("Отсутствующий в состоянии ресурс ошибочно считается найденным")

        with (
            patch.object(opentofu_module, "STATE_FILE", state_file),
            patch.object(
                opentofu_module,
                "run",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout="[]",
                ),
            ),
        ):
            try:
                opentofu_module._state_status(directory, target, env)
            except InfraManagerError:
                pass
            else:
                fail("OpenTofu state с неверным типом корня был принят")

        with (
            patch.object(opentofu_module, "STATE_FILE", state_file),
            patch.object(
                opentofu_module,
                "run",
                side_effect=InfraManagerError("ошибка чтения состояния"),
            ),
        ):
            try:
                opentofu_module._state_status(directory, target, env)
            except InfraManagerError:
                pass
            else:
                fail("Ошибка чтения существующего состояния была скрыта")


def check_guest_summary() -> None:
    context = DeploymentContext(
        client=SimpleNamespace(),
        vmid=410,
        name="ai-control",
        node="pve",
        kind="vm",
        features=(),
        template_vmid=9000,
        address="192.168.9.41",
        target='proxmox_virtual_environment_vm.guest["410"]',
        workspace=SimpleNamespace(),
        paths=SimpleNamespace(),
    )

    def git_result(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        if argv[-2:] == ["branch", "--show-current"]:
            return SimpleNamespace(
                returncode=0,
                stdout="feature/bootstrap-990\n",
            )
        if argv[-3:] == ["rev-parse", "--short", "HEAD"]:
            return SimpleNamespace(
                returncode=0,
                stdout="abc1234\n",
            )
        fail(f"Неожиданная Git-команда итогового вывода: {argv}")
        raise AssertionError

    ok_messages: list[str] = []
    with (
        patch.object(
            guest_deploy_module,
            "run",
            side_effect=git_result,
        ),
        patch("builtins.print") as mocked_print,
        patch.object(
            guest_deploy_module,
            "console",
            SimpleNamespace(ok=ok_messages.append),
        ),
    ):
        _show_guest_summary(ROOT, context)

    output = "\n".join(
        str(call.args[0]) if call.args else ""
        for call in mocked_print.call_args_list
    )
    for expected in (
        "Гость 410 ai-control готов",
        "Тип:     VM",
        "Адрес:   192.168.9.41",
        "Узел:    pve",
        "Ветка:   feature/bootstrap-990",
        "Версия:  abc1234",
        "Развёртывание завершено без ошибок",
    ):
        if expected not in output:
            fail(f"Итог развёртывания не содержит: {expected}")

    if "Настройка ОС через Ansible завершена" not in ok_messages:
        fail("Итог развёртывания не подтверждает настройку Ansible")
    if "Semaphore" in output:
        fail("Обычный итог гостя не должен содержать сведения о Semaphore")


def check_910_self_update_path() -> None:
    context = DeploymentContext(
        client=SimpleNamespace(),
        vmid=910,
        name="infra-manager",
        node="pve",
        kind="lxc",
        features=("container-host",),
        template_vmid=None,
        address="192.168.9.10",
        target='proxmox_virtual_environment_container.guest["910"]',
        workspace=SimpleNamespace(),
        paths=SimpleNamespace(),
    )
    configured: list[dict[str, object]] = []
    validated: list[int] = []
    access_calls: list[tuple[str, int, str, str]] = []

    def record_configure(
        deployment: DeploymentContext,
        **kwargs: object,
    ) -> None:
        if deployment is not context:
            fail("Самообновление передало неожиданный контекст в Ansible")
        configured.append(kwargs)

    def record_validate(deployment: DeploymentContext) -> None:
        validated.append(deployment.vmid)

    def record_access(
        node: str,
        vmid: int,
        *,
        hostname: str,
        public_key: str,
    ) -> None:
        access_calls.append((node, vmid, hostname, public_key))

    with tempfile.TemporaryDirectory() as tmp:
        public_key = Path(tmp) / "guest_ed25519.pub"
        public_key.write_text("ssh-ed25519 AAAATEST", encoding="utf-8")

        with (
            patch.object(guest_deploy_module, "require_command"),
            patch.object(
                guest_deploy_module,
                "run",
                return_value=SimpleNamespace(returncode=0, stdout=""),
            ),
            patch.object(
                guest_deploy_module,
                "_project_branch",
                return_value="feature/openbao-ssh-client-trust",
            ),
            patch.object(
                guest_deploy_module,
                "_prepare_self_update_workspace",
                return_value=SimpleNamespace(),
            ),
            patch.object(
                guest_deploy_module,
                "_build_deployment_context",
                return_value=context,
            ),
            patch.object(
                guest_deploy_module,
                "_validate_existing_guest_object",
                side_effect=record_validate,
            ),
            patch.object(
                guest_deploy_module,
                "ensure_infra_self_access",
                side_effect=record_access,
            ),
            patch.object(
                guest_deploy_module,
                "_configure_guest_os",
                side_effect=record_configure,
            ),
            patch.object(
                guest_deploy_module,
                "prepare_workspace",
                side_effect=AssertionError(
                    "Самообновление 910 не должно готовить OpenTofu workspace"
                ),
            ),
            patch.object(
                guest_deploy_module,
                "apply_host_requirements",
                side_effect=AssertionError(
                    "Самообновление 910 не должно менять объект Proxmox"
                ),
            ),
            patch.object(
                guest_deploy_module,
                "PATHS",
                SimpleNamespace(ansible_public_key=public_key),
            ),
        ):
            if guest_deploy_module.run_deploy_guest(ROOT, 910) != 0:
                fail("Самообновление 910 должно завершаться успешно")

    if validated != [910]:
        fail("Самообновление должно проверить существующий объект 910")
    if access_calls != [
        ("pve", 910, "infra-manager", "ssh-ed25519 AAAATEST")
    ]:
        fail("Самообновление должно подготовить постоянный SSH-доступ 910")
    if configured != [
        {
            "provision_phase": "full",
            "self_update": True,
            "project_branch": "feature/openbao-ssh-client-trust",
        }
    ]:
        fail(
            "Самообновление 910 должно применять полный provision "
            "в безопасном режиме"
        )


def check_temporary_certificate_path() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ca = root / "ssh-client-ca.pub"
        ca.write_text("ssh-rsa AAAACA\n", encoding="utf-8")
        known_hosts = root / "known_hosts"
        permanent_key = root / "guest_ed25519"
        permanent_key.write_text("permanent", encoding="utf-8")
        context = DeploymentContext(
            client=SimpleNamespace(),
            vmid=910,
            name="infra-manager",
            node="pve",
            kind="lxc",
            features=("container-host",),
            template_vmid=None,
            address="192.168.9.10",
            target='proxmox_virtual_environment_container.guest["910"]',
            workspace=SimpleNamespace(),
            paths=DeploymentPaths(
                guest_dir=ROOT / "infrastructure/guests/910-infra-manager",
                private_key=permanent_key,
                playbook=ROOT / "automation/ansible/playbooks/configure-guest.yml",
                known_hosts=known_hosts,
                plan_file=root / "plan",
            ),
        )
        ansible_calls: list[tuple[list[str], dict[str, str]]] = []

        def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
            if argv[0] == "ssh-keygen" and "-t" in argv:
                key = Path(argv[argv.index("-f") + 1])
                key.write_text("PRIVATE", encoding="utf-8")
                Path(str(key) + ".pub").write_text(
                    "ssh-ed25519 AAAAPUBLIC temporary\n",
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if argv[:2] == ["ssh-keygen", "-L"]:
                return SimpleNamespace(returncode=0, stdout="valid", stderr="")
            if argv[0] == "ssh":
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if argv[0] == "ansible-playbook":
                ansible_calls.append((argv, kwargs["env"]))
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"Неожиданная команда: {argv!r}")

        with (
            patch.object(
                guest_deploy_module,
                "PATHS",
                SimpleNamespace(ssh_client_ca_public_key=ca),
            ),
            patch.object(guest_deploy_module, "run", side_effect=fake_run),
            patch.object(guest_deploy_module, "_ensure_ssh_host_key"),
            patch.object(
                guest_deploy_module,
                "sign_ssh_client_key",
                return_value="ssh-ed25519-cert-v01@openssh.com AAAACERT",
            ),
        ):
            guest_deploy_module._configure_guest_os(
                context,
                provision_phase="full",
                project_branch="feature/test",
            )

        if len(ansible_calls) != 1:
            fail("Ansible должен запускаться ровно один раз")
        argv, env = ansible_calls[0]
        selected = Path(argv[argv.index("--private-key") + 1])
        if selected == permanent_key:
            fail("При рабочем CA Ansible не перешёл на временный ключ")
        if "CertificateFile=" not in env["ANSIBLE_SSH_ARGS"]:
            fail("Ansible не получил временный SSH-сертификат")
        if selected.exists():
            fail("Временный закрытый ключ не удалён после Ansible")


def check_host_certificate_is_passed_to_ansible() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        client_ca = root / "ssh-client-ca.pub"
        host_ca = root / "ssh-host-ca.pub"
        client_ca.write_text("ssh-rsa AAAACLIENTCA\n", encoding="utf-8")
        host_ca.write_text("ssh-rsa AAAAHOSTCA\n", encoding="utf-8")
        permanent_key = root / "guest_ed25519"
        permanent_key.write_text("permanent", encoding="utf-8")
        context = DeploymentContext(
            client=SimpleNamespace(),
            vmid=410,
            name="ai-control",
            node="pve",
            kind="vm",
            features=(),
            template_vmid=9000,
            address="192.168.4.10",
            target='proxmox_virtual_environment_vm.guest["410"]',
            workspace=SimpleNamespace(),
            paths=DeploymentPaths(
                guest_dir=ROOT / "infrastructure/guests/410-ai-control",
                private_key=permanent_key,
                playbook=ROOT / "automation/ansible/playbooks/configure-guest.yml",
                known_hosts=root / "known_hosts",
                plan_file=root / "plan",
            ),
        )
        ansible_calls: list[list[str]] = []

        def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
            if argv[0] == "ssh-keygen" and "-t" in argv:
                key = Path(argv[argv.index("-f") + 1])
                key.write_text("PRIVATE", encoding="utf-8")
                Path(str(key) + ".pub").write_text(
                    "ssh-ed25519 AAAAPUBLIC temporary\n",
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if argv[:2] == ["ssh-keygen", "-L"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout=(
                        "Type: ssh-ed25519-cert-v01@openssh.com host certificate\n"
                        "Principals:\n"
                        "        ai-control\n"
                        "        192.168.4.10\n"
                    ),
                    stderr="",
                )
            if argv[0] == "ssh":
                if argv[-1] == "cat /etc/ssh/ssh_host_ed25519_key.pub":
                    return SimpleNamespace(
                        returncode=0,
                        stdout="ssh-ed25519 AAAAHOST root@ai-control\n",
                        stderr="",
                    )
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if argv[0] == "ansible-playbook":
                ansible_calls.append(argv)
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"Неожиданная команда: {argv!r}")

        with (
            patch.object(
                guest_deploy_module,
                "PATHS",
                SimpleNamespace(
                    ssh_client_ca_public_key=client_ca,
                    ssh_host_ca_public_key=host_ca,
                ),
            ),
            patch.object(guest_deploy_module, "run", side_effect=fake_run),
            patch.object(guest_deploy_module, "_ensure_ssh_host_key"),
            patch.object(
                guest_deploy_module,
                "sign_ssh_client_key",
                return_value="ssh-ed25519-cert-v01@openssh.com AAAACLIENTCERT",
            ),
            patch.object(
                guest_deploy_module,
                "sign_ssh_host_key",
                return_value="ssh-ed25519-cert-v01@openssh.com AAAAHOSTCERT",
            ) as sign_host,
        ):
            guest_deploy_module._configure_guest_os(context)

        sign_host.assert_called_once_with(
            "pve",
            "ssh-ed25519 AAAAHOST root@ai-control",
            vmid=410,
            hostname="ai-control",
            address="192.168.4.10",
        )
        if len(ansible_calls) != 1:
            fail("Ansible должен запускаться ровно один раз")
        argv = ansible_calls[0]
        host_vars = [
            item
            for item in argv
            if item.startswith("infra_ssh_host_certificate_file=")
        ]
        if len(host_vars) != 1:
            fail("Ansible не получил путь к host-сертификату")
        certificate_path = Path(host_vars[0].split("=", 1)[1])
        if certificate_path.exists():
            fail("Временный host-сертификат не удалён после задания")


def check_certificate_bootstrap_fallback() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ca = root / "ssh-client-ca.pub"
        ca.write_text("ssh-rsa AAAACA\n", encoding="utf-8")
        permanent_key = root / "guest_ed25519"
        permanent_key.write_text("permanent", encoding="utf-8")
        context = DeploymentContext(
            client=SimpleNamespace(),
            vmid=410,
            name="ai-control",
            node="pve",
            kind="vm",
            features=(),
            template_vmid=9000,
            address="192.168.4.10",
            target='proxmox_virtual_environment_vm.guest["410"]',
            workspace=SimpleNamespace(),
            paths=DeploymentPaths(
                guest_dir=ROOT / "infrastructure/guests/410-ai-control",
                private_key=permanent_key,
                playbook=ROOT / "automation/ansible/playbooks/configure-guest.yml",
                known_hosts=root / "known_hosts",
                plan_file=root / "plan",
            ),
        )
        ansible_calls: list[tuple[list[str], dict[str, str]]] = []

        def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
            if argv[0] == "ssh-keygen" and "-t" in argv:
                key = Path(argv[argv.index("-f") + 1])
                key.write_text("PRIVATE", encoding="utf-8")
                Path(str(key) + ".pub").write_text(
                    "ssh-ed25519 AAAAPUBLIC temporary\n",
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if argv[:2] == ["ssh-keygen", "-L"]:
                return SimpleNamespace(returncode=0, stdout="valid", stderr="")
            if argv[0] == "ssh":
                return SimpleNamespace(returncode=255, stdout="", stderr="denied")
            if argv[0] == "ansible-playbook":
                ansible_calls.append((argv, kwargs["env"]))
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"Неожиданная команда: {argv!r}")

        with (
            patch.object(
                guest_deploy_module,
                "PATHS",
                SimpleNamespace(ssh_client_ca_public_key=ca),
            ),
            patch.object(guest_deploy_module, "run", side_effect=fake_run),
            patch.object(guest_deploy_module, "_ensure_ssh_host_key"),
            patch.object(
                guest_deploy_module,
                "sign_ssh_client_key",
                return_value="ssh-ed25519-cert-v01@openssh.com AAAACERT",
            ),
        ):
            guest_deploy_module._configure_guest_os(context)

        argv, env = ansible_calls[0]
        if Path(argv[argv.index("--private-key") + 1]) != permanent_key:
            fail("Первичный гость должен сохранить переходный постоянный ключ")
        if "CertificateFile=" in env["ANSIBLE_SSH_ARGS"]:
            fail("Отклонённый сертификат не должен передаваться Ansible")


def check_certificate_failure_is_fatal_after_trust() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ca = root / "ssh-client-ca.pub"
        ca.write_text("ssh-rsa AAAACA\n", encoding="utf-8")
        permanent_key = root / "guest_ed25519"
        permanent_key.write_text("permanent", encoding="utf-8")
        context = DeploymentContext(
            client=SimpleNamespace(),
            vmid=910,
            name="infra-manager",
            node="pve",
            kind="lxc",
            features=("container-host",),
            template_vmid=None,
            address="192.168.9.10",
            target='proxmox_virtual_environment_container.guest["910"]',
            workspace=SimpleNamespace(),
            paths=DeploymentPaths(
                guest_dir=ROOT / "infrastructure/guests/910-infra-manager",
                private_key=permanent_key,
                playbook=ROOT / "automation/ansible/playbooks/configure-guest.yml",
                known_hosts=root / "known_hosts",
                plan_file=root / "plan",
            ),
        )
        ansible_called = False

        def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
            nonlocal ansible_called
            if argv[0] == "ssh-keygen" and "-t" in argv:
                key = Path(argv[argv.index("-f") + 1])
                key.write_text("PRIVATE", encoding="utf-8")
                Path(str(key) + ".pub").write_text(
                    "ssh-ed25519 AAAAPUBLIC temporary\n",
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if argv[:2] == ["ssh-keygen", "-L"]:
                return SimpleNamespace(returncode=0, stdout="valid", stderr="")
            if argv[0] == "ssh":
                if "CertificateFile=" in " ".join(argv):
                    return SimpleNamespace(
                        returncode=255,
                        stdout="",
                        stderr="certificate denied",
                    )
                return SimpleNamespace(
                    returncode=0,
                    stdout="ssh-rsa AAAACA\n",
                    stderr="",
                )
            if argv[0] == "ansible-playbook":
                ansible_called = True
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"Неожиданная команда: {argv!r}")

        with (
            patch.object(
                guest_deploy_module,
                "PATHS",
                SimpleNamespace(ssh_client_ca_public_key=ca),
            ),
            patch.object(guest_deploy_module, "run", side_effect=fake_run),
            patch.object(guest_deploy_module, "_ensure_ssh_host_key"),
            patch.object(
                guest_deploy_module,
                "sign_ssh_client_key",
                return_value="ssh-ed25519-cert-v01@openssh.com AAAACERT",
            ),
        ):
            try:
                guest_deploy_module._configure_guest_os(context)
            except InfraManagerError:
                pass
            else:
                fail("Ошибка сертификата скрыта переходным постоянным ключом")

        if ansible_called:
            fail("После установленного CA запрещён откат Ansible на постоянный ключ")


def main_test() -> None:
    check_opentofu_state_status()
    check_guest_summary()
    check_910_self_update_path()
    check_temporary_certificate_path()
    check_host_certificate_is_passed_to_ansible()
    check_certificate_bootstrap_fallback()
    check_certificate_failure_is_fatal_after_trust()
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

    def deployment_for(
        client: object,
        *,
        paths: DeploymentPaths = deployment_paths,
    ) -> DeploymentContext:
        return DeploymentContext(
            client=client,
            vmid=410,
            name="test-vm",
            node="pve",
            kind="vm",
            features=(),
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

    lxc_deployment = DeploymentContext(
        client=SimpleNamespace(
            find_vm=lambda vmid: {"type": "lxc", "name": "test-lxc"}
        ),
        vmid=910,
        name="test-lxc",
        node="pve",
        kind="lxc",
        features=("container-host",),
        template_vmid=None,
        address="192.0.2.20",
        target='proxmox_virtual_environment_container.guest["910"]',
        workspace=workspace,
        paths=deployment_paths,
    )
    if _validate_pve_and_state(
        lxc_deployment,
        state_present=True,
        state_status="ready",
    ) is not None:
        fail("Проверка состояния LXC при успехе должна возвращать None")
    if "proxmox_virtual_environment_container" not in lxc_deployment.target:
        fail("LXC использует неверный адрес ресурса OpenTofu")

    if _validate_pve_and_state(
        deployment,
        state_present=True,
        state_status="ready",
    ) is not None:
        fail("Проверка состояния VM при успехе должна возвращать None")

    invalid_state_cases = (
        (
            SimpleNamespace(
                find_vm=lambda vmid: {"type": "lxc", "name": "test-vm"}
            ),
            True,
            "ready",
            "VMID занят LXC",
        ),
        (deployment_client, False, "", "VM отсутствует в состоянии OpenTofu"),
        (SimpleNamespace(find_vm=lambda vmid: None), True, "ready", "состояние OpenTofu есть без VM"),
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
            fail(f"Проверка состояния VM приняла небезопасный случай: {description}")

    class TaintedClient:
        def __init__(self) -> None:
            self.resource = {"type": "qemu", "name": "test-vm"}
            self.tasks: list[tuple[str, str]] = []

        def find_vm(self, vmid: int):
            return self.resource

        def data(self, path: str):
            return {"status": "stopped"}

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
        fail("Повреждённая VM не была удалена после проверки типа и имени")
    if not any(command[2:4] == ["state", "rm"] for command in tainted_commands):
        fail("Повреждённая VM не была удалена из состояния OpenTofu")

    def unexpected_plan_run(argv: list[str], **kwargs: object):
        if "-json" in argv:
            payload = {
                "resource_changes": [
                    {
                        "address": 'proxmox_virtual_environment_vm.guest["999"]',
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
            fail("Целевой план разрешил изменение постороннего ресурса")

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
        fail("Целевой план неверно разобрал действия выбранной VM")

    class ProtectedTemplateClient:
        def __init__(self) -> None:
            self.protection_values: list[int] = []

        def vm_config(self, *, node: str, vmid: int) -> dict[str, int]:
            return {"template": 1, "protection": 1}

        def put(self, path: str, *, form: dict[str, int]) -> None:
            self.protection_values.append(form["protection"])

    protected_client = ProtectedTemplateClient()

    def failed_apply(argv: list[str], **kwargs: object):
        raise InfraManagerError("ожидаемая ошибка применения")

    with patch.object(guest_deploy_module, "run", failed_apply):
        try:
            _apply_plan(deployment_for(protected_client), ["create"])
        except InfraManagerError:
            pass
        else:
            fail("Ожидалась ошибка tofu apply")
    if protected_client.protection_values != [0, 1]:
        fail("Защита шаблона не была восстановлена после ошибки tofu apply")

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
            side_effect=InfraManagerError("ожидаемая ошибка построения плана"),
        ):
            try:
                _reconcile_guest_infrastructure(
                    deployment_for(deployment_client, paths=temporary_paths)
                )
            except InfraManagerError:
                pass
            else:
                fail("Ожидалась ошибка построения плана OpenTofu")
        if plan_file.exists():
            fail("Временный план OpenTofu не был удалён после ошибки")

    guest_dir = _find_guest_directory(ROOT, 410)
    if guest_dir.name != "410-ai-control":
        fail(f"Для VM 410 найден неожиданный каталог: {guest_dir}")

    print("Проверки развёртывания гостевых систем infra-manager пройдены.")


if __name__ == "__main__":
    main_test()
