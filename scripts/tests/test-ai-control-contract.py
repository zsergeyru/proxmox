#!/usr/bin/env python3
"""Контрактные и дымовые проверки минимального AI Control."""

from __future__ import annotations

import base64
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
GUEST_LAYOUT = (
    ROOT / "automation" / "ansible" / "roles" / "guest_layout" / "tasks" / "main.yml"
)
UNIT = GUEST / "rootfs" / "etc" / "systemd" / "system" / "ai-control.service"
CORE = GUEST / "rootfs" / "opt" / "ai-control" / "core" / "bin" / "ai-control"
WEB = GUEST / "rootfs" / "opt" / "ai-control" / "core" / "web" / "index.html"
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


def auth_header(username: str, password: str) -> str:
    raw = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {raw}"


guest = load_yaml(GUEST_MANIFEST)
provision = load_yaml(PROVISION)

assert guest["vmid"] == 410
assert guest["name"] == "ai-control"
assert guest["role"] == "ai-control"
assert guest["pve_management"] is False

ai = provision["ai_control"]
assert ai["user"] == "ai-control"
assert ai["group"] == "ai-control"
assert ai["agent"]["primary"] == "hermes"
assert ai["agent"]["replaceable"] is True
assert ai["web"]["required"] is True
assert ai["web"]["port"] == 4100
assert ai["web"]["username"] == "admin"
assert ai["web"]["authentication"] == "basic"

paths = ai["paths"]
expected_paths = {
    "root": "/opt/ai-control",
    "core": "/opt/ai-control/core",
    "core_bin": "/opt/ai-control/core/bin",
    "web_root": "/opt/ai-control/core/web",
    "agents": "/opt/ai-control/agents",
    "primary_agent": "/opt/ai-control/agents/hermes",
    "repositories": "/opt/ai-control/repos",
    "project_repository": "/opt/ai-control/repos/proxmox",
    "instructions": "/opt/ai-control/instructions",
    "tools": "/opt/ai-control/tools",
    "state": "/opt/ai-control/state",
    "configuration": "/etc/ai-control",
    "runtime": "/run/ai-control",
    "cache": "/var/cache/ai-control",
    "models_config": "/etc/ai-control/models.json",
    "tools_config": "/etc/ai-control/tools.json",
    "web_credentials": "/etc/ai-control/web.env",
}
assert paths == expected_paths

executor = provision["integrations"]["infrastructure_executor"]
assert executor["role"] == "infra-manager"
assert executor["service"] == "semaphore"
assert "guest_vmid" not in executor

persistence = provision["persistence"]
assert persistence["backup_required"] == ["/opt/ai-control/state"]
assert "/etc/proxmox-guest/ssh" not in persistence["backup_required"]
assert "/run/ai-control" in persistence["ephemeral"]
assert "/var/cache/ai-control" in persistence["ephemeral"]
assert "/etc/ai-control/web.env" not in persistence["reproducible"]

playbook_text = PLAYBOOK.read_text(encoding="utf-8")
assert "name: ai_control" in playbook_text
assert '(guest_manifest.role | default(\'\')) == "ai-control"' in playbook_text
assert "guest_manifest.vmid == 410" not in playbook_text
assert "guest_manifest.vmid | int == 410" not in playbook_text

role_text = ROLE.read_text(encoding="utf-8")
assert "provision.ai_control.paths" in role_text
assert "provision.ai_control.service.executable" in role_text
assert "secrets.token_urlsafe(24)" in role_text
assert "web.env" in role_text
assert "guest_manifest.vmid" not in role_text
assert "guest_vmid" not in role_text
assert "910" not in role_text

guest_layout_text = GUEST_LAYOUT.read_text(encoding="utf-8")
assert "project_repository" not in guest_layout_text
assert "components.agents" not in guest_layout_text

unit_text = UNIT.read_text(encoding="utf-8")
assert "User=ai-control" in unit_text
assert "Group=ai-control" in unit_text
assert "ConditionPathIsExecutable=/opt/ai-control/core/bin/ai-control" in unit_text
assert "ExecStart=/opt/ai-control/core/bin/ai-control" in unit_text
assert "EnvironmentFile=/etc/ai-control/web.env" in unit_text
assert "ProtectSystem=strict" in unit_text
assert "/opt/ai-control/repos/proxmox" in unit_text
assert "NoNewPrivileges=true" in unit_text

models_data = json.loads(MODELS.read_text(encoding="utf-8"))
tools_data = json.loads(TOOLS.read_text(encoding="utf-8"))
assert models_data["schema_version"] == 1
assert models_data["models"] == []
assert tools_data["schema_version"] == 1
assert {"tasks", "memory"}.issubset(
    {item["id"] for item in tools_data["tools"] if item["status"] == "available"}
)
assert "<title>AI Control</title>" in WEB.read_text(encoding="utf-8")

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
    assert store.list_memory()[0]["key"] == "room"

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

    web_root = tmp / "web"
    web_root.mkdir()
    (web_root / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    models_file = tmp / "models.json"
    tools_file = tmp / "tools.json"
    models_file.write_text('{"models":[]}', encoding="utf-8")
    tools_file.write_text('{"tools":[]}', encoding="utf-8")

    settings = SimpleNamespace(
        state=tmp,
        web_root=web_root,
        models_file=models_file,
        tools_file=tools_file,
        primary_agent="hermes",
        listen="127.0.0.1",
        port=0,
        web_user="admin",
        web_password="test-secret",
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
        urllib.request.urlopen(f"{base}/api/status", timeout=2)
    except urllib.error.HTTPError as exc:
        assert exc.code == 401
    else:
        raise AssertionError("API должен требовать аутентификацию")

    request = urllib.request.Request(
        f"{base}/api/status",
        headers={"Authorization": auth_header("admin", "test-secret")},
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        status = json.load(response)
    assert status["service"] == "ai-control"
    assert status["primary_agent"]["status"] == "not_connected"

    server.shutdown()
    server.server_close()
    thread.join(timeout=2)

print("[ОК] AI Control base contract")
