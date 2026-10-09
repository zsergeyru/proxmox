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
from infra_manager import guest_deploy_infrastructure as guest_infra_module
from infra_manager import opentofu as opentofu_module
from infra_manager.common import InfraManagerError
from infra_manager.guest_operations import extract_survey_vmid
from infra_manager.guest_deploy import (
    DeploymentContext,
    DeploymentPaths,
    _apply_plan,
    _build_guest_plan,
    _find_guest_directory,
    _reconcile_guest_infrastructure,
    _validate_pve_and_state,
)
from infra_manager.opentofu import OpenTofuWorkspace


def fail(message: str) -> None:
    raise SystemExit(message)


def check_deploy_guest_survey_input() -> None:
    remaining, vmid = extract_survey_vmid(["GUEST_VMID=410"])
    if remaining != [] or vmid != 410:
        fail("Guest operation неверно разбирает survey-переменную GUEST_VMID")

    remaining, vmid = extract_survey_vmid(["910"])
    if remaining != ["910"] or vmid is not None:
        fail("Позиционный VMID должен сохранять совместимость CLI")

    for invalid in (["GUEST_VMID=bad"], ["GUEST_VMID=410", "GUEST_VMID=910"]):
        try:
            extract_survey_vmid(invalid)
        except InfraManagerError:
            pass
        else:
            fail("Guest operation принял некорректную survey-переменную")


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


def check_project_branch_after_semaphore_branch_switch() -> None:
    """Semaphore может оставить имя local branch main после pull другой ветки."""

    def fake_run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        if argv[-2:] == ["branch", "--show-current"]:
            return SimpleNamespace(returncode=0, stdout="main\n")
        if "for-each-ref" in argv:
            return SimpleNamespace(
                returncode=0,
                stdout="origin/feature/guest-portals\n",
            )
        fail(f"Неожиданная Git-команда: {argv!r}")
        raise AssertionError

    with patch.object(
        guest_deploy_module,
        "run",
        side_effect=fake_run,
    ):
        branch = guest_deploy_module.project_branch_for_checkout(ROOT)

    if branch != "feature/guest-portals":
        fail(
            "Фактическая ветка задания должна определяться по remote ref HEAD, "
            "а не по устаревшему имени локальной ветки"
        )


def check_infra_manager_self_update_path_after_vmid_change() -> None:
    context = DeploymentContext(
        client=SimpleNamespace(),
        vmid=920,
        name="infra-manager",
        node="pve",
        kind="lxc",
        features=("container-host",),
        template_vmid=None,
        address="192.168.9.20",
        target='proxmox_virtual_environment_container.guest["920"]',
        workspace=SimpleNamespace(),
        paths=SimpleNamespace(),
    )
    configured: list[dict[str, object]] = []
    validated: list[int] = []

    def record_configure(
        deployment: DeploymentContext,
        **kwargs: object,
    ) -> None:
        if deployment is not context:
            fail("Самообновление передало неожиданный контекст в Ansible")
        configured.append(kwargs)

    def record_validate(deployment: DeploymentContext) -> None:
        validated.append(deployment.vmid)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        guest_dir = root / "infrastructure/guests/920-infra-manager"
        guest_dir.mkdir(parents=True)
        (guest_dir / "guest.yaml").write_text(
            "schema_version: 12\n"
            "vmid: 920\n"
            "name: infra-manager\n"
            "role: infra-manager\n"
            "profile: debian-lxc-docker\n"
            "pve_management: false\n",
            encoding="utf-8",
        )
        with (
            patch.object(guest_deploy_module, "require_command"),
            patch.object(
                guest_deploy_module,
                "run",
                return_value=SimpleNamespace(returncode=0, stdout=""),
            ),
            patch.object(
                guest_deploy_module,
                "project_branch_for_checkout",
                return_value="feature/infra-manager-role",
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
                "_configure_guest_os",
                side_effect=record_configure,
            ),
            patch.object(
                guest_deploy_module,
                "prepare_workspace",
                side_effect=AssertionError(
                    "Самообновление infra-manager не должно готовить OpenTofu workspace"
                ),
            ),
            patch.object(
                guest_deploy_module,
                "apply_host_requirements",
                side_effect=AssertionError(
                    "Самообновление infra-manager не должно менять объект Proxmox"
                ),
            ),
        ):
            if guest_deploy_module.run_deploy_guest(root, 920) != 0:
                fail("Самообновление infra-manager с VMID 920 должно завершаться успешно")

    if validated != [920]:
        fail("Самообновление должно проверить существующий объект infra-manager")
    if configured != [
        {
            "provision_phase": "full",
            "self_update": True,
            "project_branch": "feature/infra-manager-role",
            "allow_legacy_bootstrap": False,
        }
    ]:
        fail(
            "Самообновление infra-manager должно применять полный provision "
            "в безопасном режиме"
        )

def check_infra_manager_self_update_points_to_recovery() -> None:
    context = DeploymentContext(
        client=SimpleNamespace(),
        vmid=920,
        name="infra-manager",
        node="pve",
        kind="lxc",
        features=("container-host",),
        template_vmid=None,
        address="192.0.2.20",
        target='proxmox_virtual_environment_container.guest["920"]',
        workspace=SimpleNamespace(),
        paths=SimpleNamespace(),
    )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        guest_dir = root / "infrastructure/guests/920-infra-manager"
        guest_dir.mkdir(parents=True)
        (guest_dir / "guest.yaml").write_text(
            "schema_version: 12\n"
            "vmid: 920\n"
            "name: infra-manager\n"
            "role: infra-manager\n"
            "profile: debian-lxc-docker\n"
            "pve_management: false\n",
            encoding="utf-8",
        )

        with (
            patch.object(guest_deploy_module, "require_command"),
            patch.object(
                guest_deploy_module,
                "run",
                return_value=SimpleNamespace(returncode=0, stdout=""),
            ),
            patch.object(
                guest_deploy_module,
                "project_branch_for_checkout",
                return_value="feature/unified-guest-deploy",
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
                side_effect=InfraManagerError("гость недоступен"),
            ),
            patch.object(
                guest_deploy_module,
                "_configure_guest_os",
                side_effect=AssertionError(
                    "Ansible не должен запускаться после ошибки preflight"
                ),
            ),
        ):
            try:
                guest_deploy_module.run_deploy_guest(root, 920)
            except InfraManagerError as exc:
                message = str(exc)
                if "infra-manager recover" not in message:
                    fail(
                        "Ошибка штатного deploy infra-manager должна явно "
                        "направлять в recovery"
                    )
            else:
                fail("Недоступный infra-manager не должен считаться обновлённым")


def check_openbao_machine_identity_preparation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ca = root / "ca.crt"
        ca.write_text("TEST-CA\n", encoding="utf-8")
        host_ca = root / "ssh-host-ca.pub"
        host_ca.write_text("ssh-ed25519 AAAAHOSTCA\n", encoding="utf-8")
        private_key = root / "guest_ed25519"
        private_key.write_text("PRIVATE", encoding="utf-8")
        context = DeploymentContext(
            client=SimpleNamespace(),
            vmid=910,
            name="infra-manager",
            node="pve",
            kind="lxc",
            features=(),
            template_vmid=None,
            address="192.168.9.10",
            target='proxmox_virtual_environment_container.guest["910"]',
            workspace=SimpleNamespace(),
            paths=DeploymentPaths(
                guest_dir=ROOT / "infrastructure/guests/910-infra-manager",
                private_key=private_key,
                playbook=ROOT / "automation/ansible/playbooks/configure-guest.yml",
                known_hosts=root / "known_hosts",
                plan_file=root / "plan",
            ),
        )

        with (
            patch.object(
                guest_deploy_module,
                "PATHS",
                SimpleNamespace(
                    openbao_tls_ca=ca,
                    ssh_host_ca_public_key=host_ca,
                ),
            ),
            patch.object(
                guest_deploy_module,
                "run",
                return_value=SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="",
                ),
            ),
            patch.object(
                guest_deploy_module,
                "issue_openbao_machine_credentials",
                return_value={
                    "role_id": "role-910",
                    "secret_id": "secret-910",
                },
            ) as issue_credentials,
        ):
            args = guest_deploy_module._prepare_openbao_machine_ansible_vars(
                context,
                private_key=private_key,
                certificate=None,
                directory=root,
            )

        issue_credentials.assert_called_once_with("pve", 910)
        if "infra_openbao_machine_enabled=true" not in args:
            fail("OTP-источник 910 не получил признак машинной identity")
        if "infra_openbao_otp_target_enabled=false" not in args:
            fail("OTP-источник 910 неожиданно стал целью")
        if f"infra_openbao_ca_file={ca}" not in args:
            fail("OTP-источник не получил TLS CA OpenBao")
        if f"infra_openbao_ssh_host_ca_file={host_ca}" not in args:
            fail("OTP-источник не получил публичный SSH host CA")
        target_args = [
            item
            for item in args
            if item.startswith("infra_openbao_otp_target_ips=")
        ]
        if target_args != ["infra_openbao_otp_target_ips=192.168.4.10"]:
            fail(f"OTP-источник получил неверные SSH-цели: {target_args!r}")
        env_args = [
            item
            for item in args
            if item.startswith("infra_openbao_machine_env_file=")
        ]
        if len(env_args) != 1:
            fail("Не подготовлен временный machine.env")
        env_file = Path(env_args[0].split("=", 1)[1])
        content = env_file.read_text(encoding="utf-8")
        for expected in (
            "OPENBAO_ADDR=https://192.168.9.10:8202",
            "OPENBAO_ROLE_ID=role-910",
            "OPENBAO_SECRET_ID=secret-910",
            "OPENBAO_CA=/etc/infra-manager/openbao/ca.crt",
            "OPENBAO_SSH_OTP_ROLE=guest-910",
        ):
            if expected not in content:
                fail(f"machine.env не содержит {expected}")
        if env_file.stat().st_mode & 0o777 != 0o600:
            fail("Временный machine.env должен иметь права 0600")
        if any("secret-910" in item for item in args):
            fail("SecretID не должен попадать в argv Ansible")

        with (
            patch.object(
                guest_deploy_module,
                "PATHS",
                SimpleNamespace(
                    openbao_tls_ca=ca,
                    ssh_host_ca_public_key=host_ca,
                ),
            ),
            patch.object(
                guest_deploy_module,
                "run",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout="",
                    stderr="",
                ),
            ),
            patch.object(
                guest_deploy_module,
                "issue_openbao_machine_credentials",
            ) as issue_existing,
        ):
            existing_args = (
                guest_deploy_module._prepare_openbao_machine_ansible_vars(
                    context,
                    private_key=private_key,
                    certificate=None,
                    directory=root,
                )
            )

        issue_existing.assert_not_called()
        if any(
            item.startswith("infra_openbao_machine_env_file=")
            for item in existing_args
        ):
            fail("Существующая machine identity не должна перевыпускаться")

        ca.unlink()
        with (
            patch.object(
                guest_deploy_module,
                "PATHS",
                SimpleNamespace(
                    openbao_tls_ca=ca,
                    ssh_host_ca_public_key=host_ca,
                ),
            ),
            patch.object(
                guest_deploy_module,
                "read_openbao_tls_ca",
                return_value="-----BEGIN CERTIFICATE-----\nTEST\n-----END CERTIFICATE-----\n",
            ) as read_ca,
            patch.object(
                guest_deploy_module,
                "run",
                return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
            ),
        ):
            fallback_args = guest_deploy_module._prepare_openbao_machine_ansible_vars(
                context,
                private_key=private_key,
                certificate=None,
                directory=root,
            )
        read_ca.assert_called_once_with("pve")
        staged_ca = root / "openbao-ca.crt"
        if f"infra_openbao_ca_file={staged_ca}" not in fallback_args:
            fail("TLS CA с PVE не передан в Ansible")
        if staged_ca.stat().st_mode & 0o777 != 0o600:
            fail("Временный TLS CA должен иметь права 0600")



def check_openbao_target_only_preparation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ca = root / "ca.crt"
        ca.write_text("TEST-CA\n", encoding="utf-8")
        private_key = root / "guest_ed25519"
        private_key.write_text("PRIVATE", encoding="utf-8")
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
                private_key=private_key,
                playbook=ROOT / "automation/ansible/playbooks/configure-guest.yml",
                known_hosts=root / "known_hosts",
                plan_file=root / "plan",
            ),
        )

        with (
            patch.object(
                guest_deploy_module,
                "PATHS",
                SimpleNamespace(openbao_tls_ca=ca),
            ),
            patch.object(
                guest_deploy_module,
                "issue_openbao_machine_credentials",
            ) as issue_credentials,
        ):
            args = guest_deploy_module._prepare_openbao_machine_ansible_vars(
                context,
                private_key=private_key,
                certificate=None,
                directory=root,
            )

        issue_credentials.assert_not_called()
        if "infra_openbao_machine_enabled=false" not in args:
            fail("OTP target-only гость неожиданно получил машинную identity")
        if "infra_openbao_otp_target_enabled=true" not in args:
            fail("OTP target-only гость не получил verifier")
        if "infra_openbao_otp_target_ip=192.168.4.10" not in args:
            fail("OTP target-only гость получил неверный собственный адрес")
        if "infra_openbao_otp_allowed_roles=guest-910" not in args:
            fail("OTP target-only гость получил неверные разрешённые роли")
        if any(
            item.startswith("infra_openbao_machine_env_file=")
            for item in args
        ):
            fail("OTP target-only гость не должен получать machine.env")
        if any(
            item.startswith("infra_openbao_otp_target_ips=")
            for item in args
        ):
            fail("OTP target-only гость не должен получать source SSH-цели")

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
        ansible_calls: list[tuple[list[str], dict[str, str], Path | None]] = []

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
                ansible_calls.append((argv, kwargs["env"], kwargs.get("cwd")))
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
                "install_openbao_host_support",
            ),
            patch.object(
                guest_deploy_module,
                "install_recovery_host_support",
            ),
            patch.object(
                guest_deploy_module,
                "_prepare_openbao_machine_ansible_vars",
                return_value=["-e", "infra_openbao_machine_enabled=false"],
            ),
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
        argv, env, cwd = ansible_calls[0]
        selected = Path(argv[argv.index("--private-key") + 1])
        if selected == permanent_key:
            fail("При рабочем CA Ansible не перешёл на временный ключ")
        if "CertificateFile=" not in env["ANSIBLE_SSH_ARGS"]:
            fail("Ansible не получил временный SSH-сертификат")
        if env.get("ANSIBLE_CONFIG") != str(ROOT / "ansible.cfg"):
            fail("Ansible должен явно использовать ansible.cfg текущей копии проекта")
        if cwd != ROOT:
            fail("Ansible должен запускаться из корня текущей копии проекта")
        if "infra_project_git_read=false" not in argv:
            fail("910 не должен получать постоянную гостевую копию Git credential")
        expected_ca_var = f"infra_ssh_client_ca_file={ca}"
        if expected_ca_var not in argv:
            fail("Ansible не получил фактический путь клиентского SSH CA")
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
        github_key = root / "github_proxmox_repo_ed25519"
        github_key.write_text("PRIVATE-GIT-KEY", encoding="utf-8")
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
                    github_key=github_key,
                ),
            ),
            patch.object(guest_deploy_module, "run", side_effect=fake_run),
            patch.object(guest_deploy_module, "_ensure_ssh_host_key"),
            patch.object(
                guest_deploy_module,
                "install_openbao_host_support",
            ),
            patch.object(
                guest_deploy_module,
                "install_recovery_host_support",
            ),
            patch.object(
                guest_deploy_module,
                "_prepare_openbao_machine_ansible_vars",
                return_value=["-e", "infra_openbao_machine_enabled=false"],
            ),
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
        if "infra_project_git_read=true" not in argv:
            fail("410 должен получить Git read-доступ из access.yaml")
        expected_git_var = f"infra_project_git_private_key_file={github_key}"
        if expected_git_var not in argv:
            fail("Ansible не получил путь к материализованному Git credential")
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
        ca.write_text("ssh-ed25519 AAAACA\n", encoding="utf-8")
        bootstrap_key = root / "bootstrap_ed25519"
        bootstrap_key.write_text("BOOTSTRAP", encoding="utf-8")
        github_key = root / "github_proxmox_repo_ed25519"
        github_key.write_text("PRIVATE-GIT-KEY", encoding="utf-8")
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
                private_key=None,
                playbook=ROOT / "automation/ansible/playbooks/configure-guest.yml",
                known_hosts=root / "known_hosts",
                plan_file=root / "plan",
            ),
        )
        ansible_calls: list[tuple[list[str], dict[str, str]]] = []
        certificate_checks = 0

        def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
            nonlocal certificate_checks
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
                    certificate_checks += 1
                    return SimpleNamespace(
                        returncode=0 if certificate_checks >= 2 else 255,
                        stdout="",
                        stderr="",
                    )
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if argv[0] == "ansible-playbook":
                ansible_calls.append((argv, kwargs["env"]))
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"Неожиданная команда: {argv!r}")

        with (
            patch.object(
                guest_deploy_module,
                "PATHS",
                SimpleNamespace(
                    ssh_client_ca_public_key=ca,
                    github_key=github_key,
                ),
            ),
            patch.object(guest_deploy_module, "run", side_effect=fake_run),
            patch.object(guest_deploy_module, "_ensure_ssh_host_key"),
            patch.object(
                guest_deploy_module,
                "install_openbao_host_support",
            ),
            patch.object(
                guest_deploy_module,
                "install_recovery_host_support",
            ),
            patch.object(
                guest_deploy_module,
                "_prepare_openbao_machine_ansible_vars",
                return_value=["-e", "infra_openbao_machine_enabled=false"],
            ),
            patch.object(
                guest_deploy_module,
                "sign_ssh_client_key",
                return_value="ssh-ed25519-cert-v01@openssh.com AAAACERT",
            ),
            patch.object(
                guest_deploy_module,
                "_remove_obsolete_guest_authorizations",
            ),
        ):
            guest_deploy_module._configure_guest_os(
                context,
                bootstrap_private_key=bootstrap_key,
            )

        if len(ansible_calls) != 2:
            fail("Bootstrap должен дать один base-запуск и один сертификатный запуск")
        first_argv, first_env = ansible_calls[0]
        if Path(first_argv[first_argv.index("--private-key") + 1]) != bootstrap_key:
            fail("Первичная установка CA должна использовать одноразовый bootstrap key")
        if "provision_phase=base" not in first_argv:
            fail("Bootstrap должен применять только базовую настройку")
        if "CertificateFile=" in first_env["ANSIBLE_SSH_ARGS"]:
            fail("Bootstrap-вход не должен притворяться сертификатным")

        second_argv, second_env = ansible_calls[1]
        if Path(second_argv[second_argv.index("--private-key") + 1]) == bootstrap_key:
            fail("После установки CA основной Ansible не должен использовать bootstrap key")
        if "CertificateFile=" not in second_env["ANSIBLE_SSH_ARGS"]:
            fail("После bootstrap основной Ansible обязан использовать сертификат")
        if certificate_checks < 2:
            fail("После bootstrap сертификат должен быть проверен повторно")


def check_certificate_failure_is_fatal_after_trust() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ca = root / "ssh-client-ca.pub"
        ca.write_text("ssh-ed25519 AAAACA\n", encoding="utf-8")
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
                private_key=None,
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
                return SimpleNamespace(
                    returncode=255,
                    stdout="",
                    stderr="certificate denied",
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
                "install_openbao_host_support",
            ),
            patch.object(
                guest_deploy_module,
                "install_recovery_host_support",
            ),
            patch.object(
                guest_deploy_module,
                "sign_ssh_client_key",
                return_value="ssh-ed25519-cert-v01@openssh.com AAAACERT",
            ),
        ):
            try:
                guest_deploy_module._configure_guest_os(
                    context,
                    allow_legacy_bootstrap=False,
                )
            except InfraManagerError:
                pass
            else:
                fail("Ошибка сертификата была скрыта без bootstrap-доступа")

        if ansible_called:
            fail("Без bootstrap-доступа запрещён откат Ansible на постоянный ключ")

def check_pve_host_support_before_signing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ca = root / "ssh-client-ca.pub"
        ca.write_text("ssh-ed25519 AAAACA\n", encoding="utf-8")
        permanent_key = root / "guest_ed25519"
        permanent_key.write_text("permanent", encoding="utf-8")
        context = DeploymentContext(
            client=SimpleNamespace(),
            vmid=920,
            name="infra-manager",
            node="pve",
            kind="lxc",
            features=("container-host",),
            template_vmid=None,
            address="192.0.2.20",
            target='proxmox_virtual_environment_container.guest["920"]',
            workspace=SimpleNamespace(),
            paths=DeploymentPaths(
                guest_dir=ROOT / "infrastructure/guests/910-infra-manager",
                private_key=permanent_key,
                playbook=ROOT / "automation/ansible/playbooks/configure-guest.yml",
                known_hosts=root / "known_hosts",
                plan_file=root / "plan",
            ),
        )
        calls: list[str] = []

        def record_openbao(node: str, repo_root: Path) -> None:
            assert node == "pve"
            assert repo_root == ROOT
            calls.append("openbao-support")

        def record_recovery(node: str, repo_root: Path) -> None:
            assert node == "pve"
            assert repo_root == ROOT
            calls.append("recovery-support")

        def sign_client(node: str, public_key: str) -> str:
            assert node == "pve"
            assert public_key.startswith("ssh-ed25519 ")
            calls.append("sign-client")
            return "ssh-ed25519-cert-v01@openssh.com AAAACERT"

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
                calls.append("ansible")
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
                "install_openbao_host_support",
                side_effect=record_openbao,
            ),
            patch.object(
                guest_deploy_module,
                "install_recovery_host_support",
                side_effect=record_recovery,
            ),
            patch.object(
                guest_deploy_module,
                "sign_ssh_client_key",
                side_effect=sign_client,
            ),
            patch.object(
                guest_deploy_module,
                "_prepare_openbao_machine_ansible_vars",
                return_value=["-e", "infra_openbao_machine_enabled=false"],
            ),
            patch.object(
                guest_deploy_module,
                "_project_git_ansible_vars",
                return_value=["-e", "infra_project_git_read=false"],
            ),
        ):
            guest_deploy_module._configure_guest_os(
                context,
                provision_phase="full",
                self_update=True,
            )

        if calls[:3] != [
            "openbao-support",
            "recovery-support",
            "sign-client",
        ]:
            fail(
                "OpenBao и recovery helper должны обновляться до первого "
                f"SSH-подписания: {calls!r}"
            )
        if calls[-1:] != ["ansible"]:
            fail(
                "guest_deploy не должен отдельно устанавливать PVE-оболочку: "
                f"{calls!r}"
            )


def check_opentofu_provider_mirror() -> None:
    payload = b"provider-package"
    checksum = __import__("hashlib").sha256(payload).hexdigest()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        opentofu_dir = root / "opentofu"
        mirror_root = root / "mirror"
        opentofu_dir.mkdir()
        (opentofu_dir / ".terraform.lock.hcl").write_text(
            'provider "registry.opentofu.org/bpg/proxmox" {\n'
            '  version = "0.112.0"\n'
            '  hashes = [\n'
            f'    "zh:{checksum}",\n'
            '  ]\n'
            '}\n',
            encoding="utf-8",
        )

        filename = (
            "terraform-provider-proxmox_0.112.0_linux_amd64.zip"
        )
        package = (
            mirror_root
            / "registry.opentofu.org"
            / "bpg"
            / "proxmox"
            / filename
        )
        package.parent.mkdir(parents=True)
        package.write_bytes(payload)

        with (
            patch.object(
                opentofu_module,
                "PROVIDER_MIRROR_DIR",
                mirror_root,
            ),
            patch.object(
                opentofu_module,
                "_provider_platform",
                return_value=("linux", "amd64"),
            ),
            patch.object(opentofu_module, "_download_text") as download_text,
            patch.object(opentofu_module, "_download_file") as download_file,
        ):
            actual = opentofu_module._prepare_proxmox_provider_mirror(
                opentofu_dir
            )
        if actual != mirror_root:
            fail("OpenTofu provider mirror вернул неверный каталог")
        download_text.assert_not_called()
        download_file.assert_not_called()

        package.unlink()

        def fake_download(_url: str, target: Path) -> None:
            target.write_bytes(payload)

        with (
            patch.object(
                opentofu_module,
                "PROVIDER_MIRROR_DIR",
                mirror_root,
            ),
            patch.object(
                opentofu_module,
                "_provider_platform",
                return_value=("linux", "amd64"),
            ),
            patch.object(
                opentofu_module,
                "_download_text",
                return_value=f"{checksum}  {filename}\n",
            ),
            patch.object(
                opentofu_module,
                "_download_file",
                side_effect=fake_download,
            ),
        ):
            opentofu_module._prepare_proxmox_provider_mirror(
                opentofu_dir
            )
        if package.read_bytes() != payload:
            fail("OpenTofu provider mirror не сохранил проверенный пакет")

        with (
            patch.object(
                opentofu_module,
                "_prepare_proxmox_provider_mirror",
                return_value=mirror_root,
            ),
            patch.object(opentofu_module, "run") as tofu_run,
        ):
            opentofu_module._init(
                opentofu_dir,
                {"SSL_CERT_FILE": "/tmp/ca.crt"},
            )
        argv = tofu_run.call_args.args[0]
        if f"-plugin-dir={mirror_root}" not in argv:
            fail("tofu init не использует локальное зеркало провайдера")


def check_ansible_duration() -> None:
    paths = DeploymentPaths(
        guest_dir=ROOT / "infrastructure/guests/410-ai-control",
        private_key=None,
        playbook=ROOT / "automation/ansible/playbooks/configure-guest.yml",
        known_hosts=Path("/tmp/test-known-hosts"),
        plan_file=Path("/tmp/test.tfplan"),
    )
    context = DeploymentContext(
        client=SimpleNamespace(),
        vmid=410,
        name="ai-control",
        node="pve",
        kind="vm",
        features=(),
        template_vmid=9000,
        address="192.0.2.10",
        target='proxmox_virtual_environment_vm.guest["410"]',
        workspace=SimpleNamespace(),
        paths=paths,
    )
    for failure in (False, True):
        with (
            patch.object(
                guest_deploy_module,
                "PATHS",
                SimpleNamespace(ssh_client_ca_public_key=Path("/nonexistent")),
            ),
            patch.object(guest_deploy_module, "_project_git_ansible_vars", return_value=[]),
            patch.object(
                guest_deploy_module,
                "run",
                side_effect=InfraManagerError("ожидаемая ошибка") if failure else None,
            ) as run,
            patch.object(
                guest_deploy_module.time,
                "monotonic",
                side_effect=[10.0, 87.5],
            ),
            patch.object(guest_deploy_module, "console") as output,
        ):
            try:
                guest_deploy_module._run_guest_ansible(
                    context,
                    private_key=Path("/tmp/test-identity"),
                    certificate=None,
                    provision_phase="full",
                    self_update=False,
                    project_branch="main",
                )
            except InfraManagerError:
                if not failure:
                    fail("Непредвиденная ошибка Ansible")
            else:
                if failure:
                    fail("Ошибка Ansible должна приводить к исключению")
            run.assert_called_once()
            output.timing.assert_called_once_with(
                "Ansible гостя 410 (full)",
                77.5,
                interrupted=failure,
            )


def main_test() -> None:
    check_deploy_guest_survey_input()
    check_ansible_duration()
    check_pve_host_support_before_signing()
    check_opentofu_state_status()
    check_opentofu_provider_mirror()
    check_project_branch_after_semaphore_branch_switch()
    check_infra_manager_self_update_path_after_vmid_change()
    check_infra_manager_self_update_points_to_recovery()
    check_openbao_machine_identity_preparation()
    check_openbao_target_only_preparation()
    check_temporary_certificate_path()
    check_host_certificate_is_passed_to_ansible()
    check_certificate_bootstrap_fallback()
    check_certificate_failure_is_fatal_after_trust()
    workspace = OpenTofuWorkspace(
        directory=Path("/tmp/opentofu"),
        state_dir=Path("/tmp/state"),
        env={
            "SSL_CERT_FILE": "/tmp/ca.crt",
            "TF_VAR_bootstrap_ssh_public_key": "ssh-ed25519 AAAATEST bootstrap",
        },
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

    for existing in (deployment, lxc_deployment):
        with (
            patch.object(existing.client, "find_vm", wraps=existing.client.find_vm) as find_vm,
            patch.object(existing.client, "put", create=True) as put,
            patch.object(existing.client, "run_task", create=True) as run_task,
            patch.object(guest_infra_module, "run") as external_run,
        ):
            try:
                _validate_pve_and_state(
                    existing,
                    state_present=True,
                    state_status="tainted",
                )
            except InfraManagerError as exc:
                message = str(exc)
                if (
                    f"{existing.vmid} {existing.name}" not in message
                    or "незавершённом или повреждённом состоянии" not in message
                    or "автоматическое удаление запрещено" not in message
                    or "Проверьте гостя в PVE" not in message
                ):
                    fail(f"Непонятное сообщение о состоянии гостя: {message}")
            else:
                fail("Развёртывание гостя с tainted должно останавливаться")
            find_vm.assert_called_once_with(existing.vmid)
            put.assert_not_called()
            run_task.assert_not_called()
            external_run.assert_not_called()

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

    with patch.object(guest_infra_module, "run", unexpected_plan_run):
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

    with patch.object(guest_infra_module, "run", target_plan_run):
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

    bootstrap_key = workspace.env.pop("TF_VAR_bootstrap_ssh_public_key")
    try:
        _apply_plan(deployment_for(protected_client), ["create"])
    except InfraManagerError:
        pass
    else:
        fail("Создание гостя без одноразового bootstrap key должно быть запрещено")
    finally:
        workspace.env["TF_VAR_bootstrap_ssh_public_key"] = bootstrap_key
    if protected_client.protection_values:
        fail("Запрет создания без bootstrap key должен срабатывать до изменения шаблона")

    def failed_apply(argv: list[str], **kwargs: object):
        raise InfraManagerError("ожидаемая ошибка применения")

    with patch.object(guest_infra_module, "run", failed_apply):
        try:
            _apply_plan(deployment_for(protected_client), ["create"])
        except InfraManagerError:
            pass
        else:
            fail("Ожидалась ошибка tofu apply")
    if protected_client.protection_values != [0, 1]:
        fail(f"Защита шаблона после ошибки tofu apply: {protected_client.protection_values!r}")

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
