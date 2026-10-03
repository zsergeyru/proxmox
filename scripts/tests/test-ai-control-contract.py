#!/usr/bin/env python3
"""Контрактные и дымовые проверки минимального AI Control."""

from __future__ import annotations

import json
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parents[2]
GUEST = ROOT / "infrastructure" / "guests" / "410-ai-control"
GUEST_MANIFEST = GUEST / "guest.yaml"
PROVISION = GUEST / "provision.yaml"
PLAYBOOK = ROOT / "automation" / "ansible" / "playbooks" / "configure-guest.yml"
ROLE = ROOT / "automation" / "ansible" / "roles" / "ai_control" / "tasks" / "main.yml"
UNIT = GUEST / "rootfs" / "etc" / "systemd" / "system" / "ai-control.service"
CORE = GUEST / "rootfs" / "opt" / "ai-control" / "core" / "bin" / "ai-control"
CUSTOM_WEB = GUEST / "rootfs" / "opt" / "ai-control" / "core" / "web" / "index.html"
MODELS = GUEST / "rootfs" / "etc" / "ai-control" / "models.json"
TOOLS = GUEST / "rootfs" / "etc" / "ai-control" / "tools.json"


def load_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), path
    return data


def load_core() -> dict:
    namespace = {"__name__": "ai_control_contract_test"}
    code = compile(CORE.read_text(encoding="utf-8"), str(CORE), "exec")
    exec(code, namespace)
    return namespace


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


guest = load_yaml(GUEST_MANIFEST)
provision = load_yaml(PROVISION)

assert guest["vmid"] == 410
assert guest["name"] == "ai-control"
assert guest["role"] == "ai-control"
assert guest["pve_management"] is False

docker = provision["docker"]
assert "docker-ce" in docker["required_packages"]
assert "docker-compose-plugin" in docker["required_packages"]

ai = provision["ai_control"]
assert ai["user"] == "ai-control"
assert ai["group"] == "ai-control"
assert ai["agent"]["primary"] == "hermes"
assert ai["agent"]["replaceable"] is True

assert ai["api"]["listen"] == "127.0.0.1"
assert ai["api"]["port"] == 4110
assert ai["api"]["openai_model_id"] == "hermes-agent"

web = ai["web"]
assert web["required"] is True
assert web["provider"] == "open-webui"
assert web["image"] == "ghcr.io/open-webui/open-webui:v0.11.4-slim"
assert web["port"] == 4100
assert web["admin_email"] == "admin@ai-control.local"

paths = ai["paths"]
expected_paths = {
    "root": "/opt/ai-control",
    "core": "/opt/ai-control/core",
    "core_bin": "/opt/ai-control/core/bin",
    "compose": "/opt/ai-control/compose",
    "agents": "/opt/ai-control/agents",
    "primary_agent": "/opt/ai-control/agents/hermes",
    "repositories": "/opt/ai-control/repos",
    "project_repository": "/opt/ai-control/repos/proxmox",
    "instructions": "/opt/ai-control/instructions",
    "tools": "/opt/ai-control/tools",
    "state": "/opt/ai-control/state",
    "open_webui_data": "/opt/ai-control/state/open-webui",
    "configuration": "/etc/ai-control",
    "runtime": "/run/ai-control",
    "cache": "/var/cache/ai-control",
    "models_config": "/etc/ai-control/models.json",
    "tools_config": "/etc/ai-control/tools.json",
    "api_credentials": "/etc/ai-control/api.env",
    "open_webui_credentials": "/etc/ai-control/open-webui.env",
}
assert paths == expected_paths
assert "open-webui" in ai["state_directories"]

executor = provision["integrations"]["infrastructure_executor"]
assert executor["role"] == "infra-manager"
assert executor["service"] == "semaphore"
assert "guest_vmid" not in executor

persistence = provision["persistence"]
assert persistence["backup_required"] == ["/opt/ai-control/state"]
assert "/opt/ai-control/compose" in persistence["reproducible"]
assert "/etc/ai-control/api.env" not in persistence["reproducible"]
assert "/etc/ai-control/open-webui.env" not in persistence["reproducible"]
assert "/run/ai-control" in persistence["ephemeral"]
assert "/var/cache/ai-control" in persistence["ephemeral"]

playbook_text = PLAYBOOK.read_text(encoding="utf-8")
assert "name: docker" in playbook_text
assert "name: ai_control" in playbook_text
assert '(guest_manifest.role | default(\'\')) == "ai-control"' in playbook_text
assert "guest_manifest.vmid == 410" not in playbook_text
assert "guest_manifest.vmid | int == 410" not in playbook_text

role_text = ROLE.read_text(encoding="utf-8")
assert "provision.ai_control.paths" in role_text
assert "ghcr.io/open-webui/open-webui" not in role_text
assert "provision.ai_control.web.image" in role_text
assert "docker\n      - compose" in role_text
assert "OPENAI_API_BASE_URL" in role_text
assert "ENABLE_OLLAMA_API" in role_text
assert "network_mode: host" in role_text
assert "secrets.token_urlsafe(36)" in role_text
assert "secrets.token_hex(32)" in role_text
assert "guest_manifest.vmid" not in role_text
assert "guest_vmid" not in role_text
assert "910" not in role_text

unit_text = UNIT.read_text(encoding="utf-8")
assert "User=ai-control" in unit_text
assert "Group=ai-control" in unit_text
assert "EnvironmentFile=/etc/ai-control/api.env" in unit_text
assert "ExecStart=/opt/ai-control/core/bin/ai-control" in unit_text
assert "ProtectSystem=strict" in unit_text
assert "NoNewPrivileges=true" in unit_text

assert not CUSTOM_WEB.exists()

models_data = json.loads(MODELS.read_text(encoding="utf-8"))
tools_data = json.loads(TOOLS.read_text(encoding="utf-8"))
assert models_data["schema_version"] == 1
assert tools_data["schema_version"] == 1

core = load_core()
Store = core["Store"]
Api = core["Api"]
Server = core["Server"]
select_model = core["select_model"]

with tempfile.TemporaryDirectory() as tmp_dir:
    tmp = Path(tmp_dir)
    store = Store(tmp / "state.sqlite3")
    task = store.create_task("Проверка", "Дымовой тест", "hermes")
    assert task["status"] == "created"
    assert store.cancel_task(task["id"])["status"] == "cancelled"
    item = store.set_memory("home", "room", "kitchen")
    assert item["value"] == "kitchen"

    assert (
        select_model(
            [
                {
                    "id": "remote",
                    "enabled": True,
                    "priority": 20,
                    "capabilities": ["chat"],
                    "privacy": ["normal"],
                },
                {
                    "id": "local",
                    "enabled": True,
                    "priority": 10,
                    "capabilities": ["chat"],
                    "privacy": ["normal", "sensitive"],
                },
            ],
            "sensitive",
            "chat",
        )["id"]
        == "local"
    )

    models_file = tmp / "models.json"
    tools_file = tmp / "tools.json"
    models_file.write_text('{"models":[]}', encoding="utf-8")
    tools_file.write_text('{"tools":[]}', encoding="utf-8")

    settings = SimpleNamespace(
        state=tmp,
        models_file=models_file,
        tools_file=tools_file,
        primary_agent="hermes",
        openai_model_id="hermes-agent",
        listen="127.0.0.1",
        port=0,
        api_key="test-api-key",
        database=tmp / "http.sqlite3",
    )
    api = Api(settings)
    server = Server(("127.0.0.1", 0), api)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    with urllib.request.urlopen(f"{base}/health", timeout=2) as response:
        assert response.status == 200

    try:
        urllib.request.urlopen(f"{base}/v1/models", timeout=2)
    except urllib.error.HTTPError as exc:
        assert exc.code == 401
    else:
        raise AssertionError("OpenAI API должен требовать Bearer token")

    request = urllib.request.Request(
        f"{base}/v1/models",
        headers=bearer("test-api-key"),
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        model_list = json.load(response)
    assert model_list["object"] == "list"
    assert model_list["data"][0]["id"] == "hermes-agent"

    request = urllib.request.Request(
        f"{base}/v1/chat/completions",
        data=json.dumps(
            {
                "model": "hermes-agent",
                "messages": [{"role": "user", "content": "Привет"}],
            }
        ).encode(),
        headers={
            **bearer("test-api-key"),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=2)
    except urllib.error.HTTPError as exc:
        assert exc.code == 503
        error = json.load(exc)
        assert error["error"]["code"] == "agent_unavailable"
    else:
        raise AssertionError("До подключения Hermes ожидается HTTP 503")

    server.shutdown()
    server.server_close()
    thread.join(timeout=2)

print("[ОК] AI Control base contract")
