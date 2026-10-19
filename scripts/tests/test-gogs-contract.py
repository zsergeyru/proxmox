#!/usr/bin/env python3
"""Проверить гостя 310, перенос данных и защиту от пустого запуска."""

from __future__ import annotations

import configparser
import importlib.util
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).resolve().parents[2]
GUEST = ROOT / "infrastructure/guests/310-gogs"
ROLE = ROOT / "automation/ansible/roles/gogs"
PREPARE = ROLE / "files/prepare_data.py"

spec = importlib.util.spec_from_file_location("gogs_prepare_data", PREPARE)
assert spec is not None and spec.loader is not None
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def example_data(folder: Path) -> Path:
    data = folder / "gogs-data"
    (data / "gogs/conf").mkdir(parents=True)
    (data / "gogs/data").mkdir(parents=True)
    (data / "git/gogs-repositories/sergey/demo.git").mkdir(parents=True)
    (data / "git/gogs-repositories/sergey/demo.git/HEAD").write_text(
        "ref: refs/heads/main\n", encoding="utf-8"
    )
    config = """RUN_MODE = prod
RUN_USER = git
[server]
DOMAIN = localhost
EXTERNAL_URL = http://localhost:3000/
SSH_PORT = 2222

[database]
TYPE = sqlite3
PATH = data/gogs.db

[repository]
ROOT = /data/git/gogs-repositories

[security]
SECRET_KEY = already-existing-real-key-that-must-not-change

[auth]
DISABLE_REGISTRATION = true

[session]
PROVIDER = file
"""
    (data / "gogs/conf/app.ini").write_text(config, encoding="utf-8")
    with sqlite3.connect(data / "gogs/data/gogs.db") as conn:
        conn.execute("CREATE TABLE user(id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO user(name) VALUES ('sergey')")
        conn.commit()
    return data


def test_data_protection() -> None:
    with tempfile.TemporaryDirectory() as folder:
        data = example_data(Path(folder))
        helper.verify_data(data, require_marker=False, require_mount=False)
        try:
            helper.verify_data(data, require_mount=False)
        except helper.DataError:
            pass
        else:
            raise AssertionError("Нельзя запускать Gogs до подтверждения переноса")

        with patch.object(helper.os.path, "ismount", return_value=True), patch.object(
            helper, "ensure_stopped"
        ), patch.object(helper.os, "chown"):
            helper.confirm(data)
        helper.verify_data(data, require_mount=False)
        with patch.object(helper.os.path, "ismount", return_value=True), patch.object(
            helper, "ensure_stopped"
        ), patch.object(helper.os, "chown"):
            helper.confirm(data)
        assert (data / ".gogs-imported").read_text() == helper.MARKER_CONTENT

        with patch.object(helper.os.path, "ismount", return_value=True), patch.object(
            helper.os, "chown"
        ):
            helper.configure(data, "git.zsergey.ru", "https://git.zsergey.ru/", 2222)
            saved = (data / "gogs/conf/app.ini").read_text(encoding="utf-8")
            helper.configure(data, "git.zsergey.ru", "https://git.zsergey.ru/", 2222)
        assert saved == (data / "gogs/conf/app.ini").read_text(encoding="utf-8")
        settings = configparser.ConfigParser(interpolation=None)
        settings.read_string(saved)
        assert settings["server"]["DOMAIN"] == "git.zsergey.ru"
        assert settings["server"]["EXTERNAL_URL"] == "https://git.zsergey.ru/"
        assert settings["server"]["START_SSH_SERVER"] == "true"
        assert settings["server"]["SSH_LISTEN_PORT"] == "2222"
        assert settings["server"]["SSH_DOMAIN"] == "192.168.3.10"
        assert settings["auth"]["REQUIRE_SIGNIN_VIEW"] == "true"
        assert settings["auth"]["DISABLE_REGISTRATION"] == "true"
        assert settings["auth"]["ENABLE_REVERSE_PROXY_AUTHENTICATION"] == "false"
        assert settings["repository"]["FORCE_PRIVATE"] == "true"
        assert settings["security"]["SECRET_KEY"] == "already-existing-real-key-that-must-not-change"
        assert (data / "gogs/conf/app.ini.before-310").is_file()
        assert "DOMAIN = localhost" in (
            data / "gogs/conf/app.ini.before-310"
        ).read_text(encoding="utf-8")

        # Никакой маркер не заменяет существующую SQLite.
        (data / "gogs/data/gogs.db").unlink()
        try:
            helper.verify_data(data, require_mount=False)
        except helper.DataError:
            pass
        else:
            raise AssertionError("Потеря SQLite должна останавливать развёртывание")


def test_gogs_compose_and_manifest() -> None:
    guest = yaml.safe_load((GUEST / "guest.yaml").read_text(encoding="utf-8"))
    provision = yaml.safe_load((GUEST / "provision.yaml").read_text(encoding="utf-8"))
    storage = provision["pve_storage"]
    gogs = provision["gogs"]
    assert guest["vmid"] == 310
    assert guest["name"] == "gogs"
    assert guest["recreate_if_missing"] is True
    assert guest["resources"]["disk_size_gb"] == 12
    assert storage["volumes"] == [
        {"name": "gogs_data", "size_gb": 30, "mountpoint": "/mnt/bindmounts/gogs-data"}
    ]
    assert storage["mounts"] == [
        {
            "slot": "mp0",
            "volume": "gogs_data",
            "directory": "310-gogs",
            "target": "/var/lib/gogs",
            "read_only": False,
        }
    ]
    assert gogs["image"].startswith("docker.io/gogs/gogs@sha256:")
    assert len(gogs["image"].split("@sha256:")[1]) == 64
    assert gogs["storage"]["require_import_before_start"] is True
    access = yaml.safe_load((ROOT / "infrastructure/security/access.yaml").read_text(encoding="utf-8"))
    targets = next(rule["targets"] for rule in access["rules"] if rule["id"] == "infra-manager-linux-guests")
    assert "guest:310" in targets
    assert gogs["network"]["ssh_public"] is False
    assert gogs["network"]["public_url"] == "https://git.zsergey.ru/"

    env = Environment(
        loader=FileSystemLoader(str(ROLE / "templates")),
        undefined=StrictUndefined,
    )
    rendered = env.get_template("docker-compose.yml.j2").render(
        provision=provision, ansible_host="192.168.3.10"
    )
    service = yaml.safe_load(rendered)["services"]["gogs"]
    assert service["image"] == gogs["image"]
    assert service["ports"] == [
        "192.168.3.10:3000:3000",
        "192.168.3.10:2222:2222",
    ]
    assert service["volumes"] == ["/var/lib/gogs:/data"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert "build" not in service

    actions = yaml.safe_load((ROLE / "tasks/main.yml").read_text(encoding="utf-8"))
    commands = [
        item["ansible.builtin.command"]["argv"]
        for item in actions
        if "ansible.builtin.command" in item
    ]
    volume_access = next(
        i for i, task in enumerate(actions)
        if task.get("name") == "Открыть владельцу Gogs доступ к корню подключённого тома"
    )
    import_check = next(
        i for i, task in enumerate(actions)
        if task.get("name") == "Проверить целостность перенесённого состояния Gogs"
    )
    assert volume_access < import_check
    check = next(cmd for cmd in commands if "--check" in cmd)
    configure = next(cmd for cmd in commands if "--configure" in cmd)
    pull = next(cmd for cmd in commands if "pull" in cmd)
    up = next(cmd for cmd in commands if "up" in cmd)
    assert commands.index(check) < commands.index(configure) < commands.index(pull) < commands.index(up)

    playbook = yaml.safe_load(
        (ROOT / "automation/ansible/playbooks/configure-guest.yml").read_text(
            encoding="utf-8"
        )
    )
    all_roles = [
        task.get("ansible.builtin.include_role", {}).get("name")
        for task in playbook[0]["tasks"]
    ]
    assert "gogs" in all_roles


def test_rewrite_rejects_duplicates() -> None:
    try:
        helper.rewrite_ini(
            "[server]\nDOMAIN = localhost\nDOMAIN = other\n",
            {"server": {"DOMAIN": "git.zsergey.ru"}},
        )
    except helper.DataError:
        pass
    else:
        raise AssertionError("Дублирующие параметры должны блокироваться")


if __name__ == "__main__":
    test_data_protection()
    test_gogs_compose_and_manifest()
    test_rewrite_rejects_duplicates()
    print("OK: Gogs 310 data, compose and recovery contract")
