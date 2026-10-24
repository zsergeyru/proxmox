"""Generic Ansible executor for declarative physical NAS provisioning.

No application-specific deployment scripts or hardcoded playbook sequence:
one role, two guarded phases and a checked provision.yaml contract.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from .common import InfraManagerError, console, run
from .physical_host_ssh import PhysicalHostSshSession


def load_nas_provision(repo_root: Path) -> dict:
    """Validate intent; only allow role task files known in the repository."""
    folder = repo_root / "infrastructure"
    path = folder / "hosts" / "nas" / "provision.yaml"
    schema_path = folder / "schemas" / "physical-nas-provision.schema.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except (OSError, UnicodeError, yaml.YAMLError, ValueError) as exc:
        raise InfraManagerError("Не удалось прочитать декларативную схему NAS") from exc
    errors = list(Draft202012Validator(schema).iter_errors(raw))
    if errors:
        raise InfraManagerError(
            "Ошибка provision.yaml NAS: "
            + ".".join(str(v) for v in errors[0].absolute_path)
            + " (значение не соответствует схеме)"
        )
    components = raw["deployment"]["components"]
    order = set(components)
    if "urbackup_client" in order and (
        "urbackup_server" not in order
        or components.index("urbackup_server") > components.index("urbackup_client")
    ):
        raise InfraManagerError("UrBackup Client требует ранее объявленный Server")
    services = raw["services"]
    if ("apps" in order and (
        "qbittorrent" not in services or not services["qbittorrent"].get("image")
    )) or ("urbackup_server" in order and "urbackup" not in services):
        raise InfraManagerError("Для выбранного компонента NAS отсутствует описание приложения")
    for name, svc in services.items():
        component = svc["component"]
        if component != "base" and component not in order:
            # Defined but disabled services must not be presented as running.
            continue
        if component == "apps" and name == "qbittorrent" and svc.get("url") != "http://{address}:8080/":
            raise InfraManagerError("qBittorrent должен использовать опубликованный порт 8080")
        if component == "urbackup_server" and name == "urbackup" and svc.get("url") != "http://127.0.0.1:55414/":
            raise InfraManagerError("UrBackup не должен публиковать админку вне localhost")
    names = [item["name"] for item in raw["shares"]]
    targets = [(item["volume"], item["directory"]) for item in raw["shares"]]
    if len(names) != len(set(names)) or len(targets) != len(set(targets)):
        raise InfraManagerError("Общие папки NAS дублируют имя или физический путь")
    return raw


def run_nas_phase(
    repo_root: Path,
    session: PhysicalHostSshSession,
    phase: str,
    *,
    provision: dict,
    policy: dict,
    verified_volumes: dict[str, object] | None = None,
    host_volumes: dict[str, dict] | None = None,
) -> None:
    """Run one declarative NAS role phase over certificate-authenticated SSH."""
    if phase not in ("base", "services"):
        raise InfraManagerError("Неизвестный этап декларативного deploy NAS")
    if phase == "services" and (not verified_volumes or not host_volumes):
        raise InfraManagerError("Нельзя разворачивать сервисы без проверки физических томов")
    if not session.host.address or session.host.name != provision["host"]:
        raise InfraManagerError("Неподтверждённый адрес физического NAS")
    playbook = repo_root / "automation" / "ansible" / "playbooks" / "deploy-nas.yml"
    inventory = repo_root / "infrastructure" / "hosts" / "nas" / "ansible-inventory.yml"
    if not playbook.is_file() or not inventory.is_file():
        raise InfraManagerError("Нет декларативного playbook или inventory NAS")

    variables: dict[str, object] = {
        "nas_deploy_phase": phase,
        "nas_provision": provision,
        "nas_backup_policy": policy,
    }
    if phase == "services":
        assert verified_volumes is not None and host_volumes is not None
        shared = verified_volumes["shared"]
        data = verified_volumes["data"]
        backup = verified_volumes["backup"]
        variables.update({
            "nas_shared_mount": shared.mountpoint,
            "nas_shared_uuid": host_volumes["shared"]["filesystem_uuid"],
            "nas_shared_ref": host_volumes["shared"]["omv_reference"],
            "nas_data_pool": host_volumes["data"]["pool"],
            "nas_data_guid": host_volumes["data"]["pool_guid"],
            "nas_data_mountpoint": data.mountpoint,
            "nas_data_ref": host_volumes["data"]["omv_reference"],
            "nas_backup_pool": host_volumes["backup"]["pool"],
            "nas_backup_guid": host_volumes["backup"]["pool_guid"],
            "nas_backup_mountpoint": backup.mountpoint,
        })
    key, ssh_args = session.ansible_parameters()
    variables.update({
        "ansible_host": session.host.address,
        "ansible_ssh_common_args": ssh_args,
        "ansible_python_interpreter": "/usr/bin/python3",
    })
    env = os.environ.copy()
    env.update({
        "ANSIBLE_HOST_KEY_CHECKING": "True",
        "ANSIBLE_RETRY_FILES_ENABLED": "False",
        "ANSIBLE_LOG_PATH": "/dev/null",
        "ANSIBLE_SSH_TRANSFER_METHOD": "piped",
        "ANSIBLE_STDOUT_CALLBACK": "infra_nas_compact",
        "ANSIBLE_CALLBACK_PLUGINS": str(
            repo_root / "automation" / "ansible" / "callback_plugins"
        ),
        "ANSIBLE_DISPLAY_SKIPPED_HOSTS": "False",
        "ANSIBLE_DEPRECATION_WARNINGS": "False",
        "ANSIBLE_NOCOLOR": "1",
    })
    console.info("Применение конфигурации NAS: deploy-nas.yml (" + phase + ")")
    result = run([
        "ansible-playbook", str(playbook), "-i", str(inventory),
        "-u", session.host.user, "--private-key", key,
        "-e", json.dumps(variables),
    ], env=env, cwd=repo_root, stream_output=True, check=False)
    if result.returncode:
        raise InfraManagerError(
            f"Декларативный Ansible NAS ({phase}) завершился с ошибкой "
            f"({result.returncode}); диски не форматировались"
        )
