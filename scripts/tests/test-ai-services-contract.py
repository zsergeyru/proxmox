#!/usr/bin/env python3
"""Контрактные проверки 420 ai-services: Speaches + Wyoming OpenAI."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
GUEST = ROOT / "infrastructure" / "guests" / "420-ai-services"
GUEST_MANIFEST = GUEST / "guest.yaml"
PROVISION = GUEST / "provision.yaml"
PLAYBOOK = ROOT / "automation" / "ansible" / "playbooks" / "configure-guest.yml"
ROLE = ROOT / "automation" / "ansible" / "roles" / "ai_services" / "tasks" / "main.yml"
COMPOSE_TEMPLATE = (
    ROOT
    / "automation"
    / "ansible"
    / "roles"
    / "ai_services"
    / "templates"
    / "docker-compose.yml.j2"
)
ALIASES_TEMPLATE = (
    ROOT
    / "automation"
    / "ansible"
    / "roles"
    / "ai_services"
    / "templates"
    / "model_aliases.json.j2"
)


def load_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), path
    return data


guest = load_yaml(GUEST_MANIFEST)
provision = load_yaml(PROVISION)

assert guest["vmid"] == 420
assert guest["name"] == "ai-services"
assert guest["profile"] == "debian-lxc-docker"
assert "role" not in guest
assert guest["resources"] == {
    "cores": 2,
    "memory_mb": 4096,
    "swap_mb": 1024,
    "disk_size_gb": 32,
}

docker = provision["docker"]
assert docker["service"]["name"] == "docker"
assert "docker-ce" in docker["required_packages"]
assert "docker-compose-plugin" in docker["required_packages"]

services = provision["ai_services"]
speaches = services["speaches"]
wyoming = services["wyoming_openai"]
paths = services["paths"]

assert speaches["image"] == "ghcr.io/speaches-ai/speaches:0.8.3-cpu"
assert speaches["container_name"] == "speaches"
assert speaches["port"] == 8000
assert speaches["stt"] == {
    "model": "Systran/faster-whisper-small",
    "alias": "stt-default",
}
assert speaches["tts"] == {
    "model": "speaches-ai/piper-ru_RU-irina-medium",
    "alias": "tts-default-ru",
}

assert wyoming["image"] == "ghcr.io/roryeckel/wyoming_openai:0.5.0"
assert wyoming["container_name"] == "wyoming-openai"
assert wyoming["port"] == 10300
assert wyoming["languages"] == ["ru"]
assert wyoming["stt_backend"] == "SPEACHES"
assert wyoming["tts_backend"] == "SPEACHES"

assert paths == {
    "root": "/opt/ai-services",
    "compose": "/opt/ai-services/compose",
    "compose_file": "/opt/ai-services/compose/docker-compose.yml",
    "configuration": "/etc/ai-services",
    "speaches_configuration": "/etc/ai-services/speaches",
    "model_aliases": "/etc/ai-services/speaches/model_aliases.json",
    "cache": "/var/cache/ai-services",
    "speaches_cache": "/var/cache/ai-services/speaches/huggingface",
}

persistence = provision["persistence"]
assert persistence["backup_required"] == []
assert persistence["reproducible"] == [
    "/opt/ai-services/compose",
    "/etc/ai-services",
]
assert "/var/cache/ai-services" in persistence["ephemeral"]

playbook = PLAYBOOK.read_text(encoding="utf-8")
assert "name: ai_services" in playbook
assert "provision.ai_services is defined" in playbook
assert "guest_manifest.vmid == 420" not in playbook
assert "guest_manifest.vmid | int == 420" not in playbook

role_text = ROLE.read_text(encoding="utf-8")
assert isinstance(yaml.safe_load(role_text), list)
assert "docker-compose.yml.j2" in role_text
assert "model_aliases.json.j2" in role_text
assert "/v1/models/{{ item }}" in role_text
assert "/health" in role_text
assert "ansible.builtin.wait_for" in role_text
assert "guest_manifest.vmid" not in role_text
assert "guest_vmid" not in role_text
assert "420" not in role_text

compose = COMPOSE_TEMPLATE.read_text(encoding="utf-8")
assert "speaches:" in compose
assert "wyoming-openai:" in compose
assert "STT_OPENAI_URL: http://speaches:8000/v1" in compose
assert "TTS_OPENAI_URL: http://speaches:8000/v1" in compose
assert "STT_BACKEND:" in compose
assert "TTS_BACKEND:" in compose
assert "condition: service_healthy" in compose
assert "/home/ubuntu/speaches/model_aliases.json:ro" in compose
assert "/home/ubuntu/.cache/huggingface/hub" in compose
assert ":latest" not in compose
assert "docker.sock" not in compose

aliases = ALIASES_TEMPLATE.read_text(encoding="utf-8")
assert "provision.ai_services.speaches.stt.alias" in aliases
assert "provision.ai_services.speaches.stt.model" in aliases
assert "provision.ai_services.speaches.tts.alias" in aliases
assert "provision.ai_services.speaches.tts.model" in aliases

print("[ОК] AI services contract: Speaches + Wyoming OpenAI")
