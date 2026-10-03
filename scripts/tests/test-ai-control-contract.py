#!/usr/bin/env python3
"""Контрактные проверки 410 AI Control: Hermes + Open WebUI."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
GUEST = ROOT / "infrastructure" / "guests" / "410-ai-control"
GUEST_MANIFEST = GUEST / "guest.yaml"
PROVISION = GUEST / "provision.yaml"
PLAYBOOK = ROOT / "automation" / "ansible" / "playbooks" / "configure-guest.yml"
ROLE = ROOT / "automation" / "ansible" / "roles" / "ai_control" / "tasks" / "main.yml"
COMPOSE_TEMPLATE = (
    ROOT
    / "automation"
    / "ansible"
    / "roles"
    / "ai_control"
    / "templates"
    / "docker-compose.yml.j2"
)
CUSTOM_CORE = GUEST / "rootfs" / "opt" / "ai-control" / "core" / "bin" / "ai-control"
CUSTOM_UNIT = (
    GUEST / "rootfs" / "etc" / "systemd" / "system" / "ai-control.service"
)
CUSTOM_MODELS = GUEST / "rootfs" / "etc" / "ai-control" / "models.json"
CUSTOM_TOOLS = GUEST / "rootfs" / "etc" / "ai-control" / "tools.json"


def load_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), path
    return data


guest = load_yaml(GUEST_MANIFEST)
provision = load_yaml(PROVISION)

assert guest["vmid"] == 410
assert guest["name"] == "ai-control"
assert guest["role"] == "ai-control"
assert guest["pve_management"] is False

docker = provision["docker"]
assert docker["service"]["name"] == "docker"
assert "docker-ce" in docker["required_packages"]
assert "docker-compose-plugin" in docker["required_packages"]

ai = provision["ai_control"]
agent = ai["agent"]
web = ai["web"]
paths = ai["paths"]

assert agent["primary"] == "hermes"
assert agent["replaceable"] is True
assert agent["image"] == "nousresearch/hermes-agent:v2026.9.14"
assert agent["container_name"] == "hermes"
assert agent["command"] == "gateway run"
assert agent["shm_size"] == "1g"
assert agent["uid"] == 10000
assert agent["gid"] == 10000
assert agent["api"] == {
    "enabled": True,
    "host": "127.0.0.1",
    "port": 8642,
    "model_name": "hermes-agent",
}
assert agent["dashboard"]["enabled"] is False

assert web["required"] is True
assert web["provider"] == "open-webui"
assert web["image"] == "ghcr.io/open-webui/open-webui:0.11.4-slim"
assert web["container_name"] == "open-webui"
assert web["port"] == 4100
assert web["admin_email"] == "admin@ai-control.local"

assert paths == {
    "root": "/opt/ai-control",
    "compose": "/opt/ai-control/compose",
    "compose_file": "/opt/ai-control/compose/docker-compose.yml",
    "state": "/opt/ai-control/state",
    "hermes_data": "/opt/ai-control/state/hermes",
    "open_webui_data": "/opt/ai-control/state/open-webui",
    "configuration": "/etc/ai-control",
    "hermes_env": "/etc/ai-control/hermes.env",
    "open_webui_env": "/etc/ai-control/open-webui.env",
}

executor = provision["integrations"]["infrastructure_executor"]
assert executor["role"] == "infra-manager"
assert executor["service"] == "semaphore"
assert "guest_vmid" not in executor

persistence = provision["persistence"]
assert persistence["backup_required"] == [
    "/opt/ai-control/state",
    "/etc/ai-control/hermes.env",
    "/etc/ai-control/open-webui.env",
]
assert persistence["reproducible"] == ["/opt/ai-control/compose"]

playbook_text = PLAYBOOK.read_text(encoding="utf-8")
assert "name: docker" in playbook_text
assert "name: ai_control" in playbook_text
assert '(guest_manifest.role | default(\'\')) == "ai-control"' in playbook_text
assert "guest_manifest.vmid == 410" not in playbook_text
assert "guest_manifest.vmid | int == 410" not in playbook_text

role_text = ROLE.read_text(encoding="utf-8")
assert isinstance(yaml.safe_load(role_text), list)
assert "API_SERVER_KEY=" in role_text
assert "API_SERVER_ENABLED=true" in role_text
assert "provision.ai_control.paths.hermes_env" in role_text
assert "provision.ai_control.paths.open_webui_env" in role_text
assert "docker-compose.yml.j2" in role_text
assert "/health" in role_text
assert "guest_manifest.vmid" not in role_text
assert "guest_vmid" not in role_text
assert "910" not in role_text

compose = COMPOSE_TEMPLATE.read_text(encoding="utf-8")
assert "services:" in compose
assert "hermes:" in compose
assert "open-webui:" in compose
assert "command: {{ provision.ai_control.agent.command }}" in compose
assert "network_mode: host" in compose
assert "shm_size: {{ provision.ai_control.agent.shm_size }}" in compose
assert 'HERMES_UID: "{{ provision.ai_control.agent.uid }}"' in compose
assert 'HERMES_GID: "{{ provision.ai_control.agent.gid }}"' in compose
assert "{{ provision.ai_control.paths.hermes_data }}:/opt/data" in compose
assert (
    "{{ provision.ai_control.paths.open_webui_data }}:/app/backend/data"
    in compose
)
assert "OPENAI_API_BASE_URL:" in compose
assert "/v1" in compose
assert "ENABLE_OLLAMA_API" in compose
assert "ENABLE_SIGNUP" in compose
assert "docker.sock" not in compose

assert not CUSTOM_CORE.exists()
assert not CUSTOM_UNIT.exists()
assert not CUSTOM_MODELS.exists()
assert not CUSTOM_TOOLS.exists()

print("[ОК] AI Control contract: Hermes + Open WebUI")
