#!/usr/bin/env python3
"""Offline tests of declarative NAS deploy/status. No SSH or disk changes."""
from __future__ import annotations

import copy
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/infra-manager"))

from infra_manager.common import InfraManagerError
from infra_manager.physical_nas_status import load_nas_status
from infra_manager.physical_nas_ansible import load_nas_provision, run_nas_phase
from infra_manager.guest_status import (
    load_declared_checks, _run_declared_check, _service_fields,
)


def refuses(action) -> None:
    try:
        action()
    except InfraManagerError:
        return
    raise AssertionError("An invalid declaration was accepted")


def test_provision_and_fail_closed_phases() -> None:
    p = load_nas_provision(ROOT)
    assert p["deployment"]["phases"] == ["base", "services"]
    assert p["deployment"]["components"] == [
        "urbackup_server", "urbackup_client", "apps", "smb",
    ]
    assert p["services"]["urbackup"]["operator"]["password"] == {
        "source": "host_file",
        "path": "/etc/nas-apps/urbackup/admin-password",
    }
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "infrastructure/hosts/nas"
        home.mkdir(parents=True)
        def check_modified(fn):
            doc = copy.deepcopy(p)
            fn(doc)
            (home / "provision.yaml").write_text(
                yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8",
            )
            refuses(lambda: load_nas_provision(Path(td)))
        check_modified(lambda d: d["deployment"]["components"].append("evil"))
        check_modified(lambda d: d["deployment"].update(phases=["services", "base"]))
        check_modified(lambda d: d["services"]["urbackup"].update(url="http://0.0.0.0:55414/"))
        check_modified(lambda d: d["services"]["qbittorrent"].update(image="untrusted:latest"))
        check_modified(lambda d: d["services"]["urbackup"]["operator"].update(
            password={"source": "host_file", "path": "/etc/shadow"}
        ))
        check_modified(lambda d: d["shares"].append(copy.deepcopy(d["shares"][0])))
        check_modified(lambda d: d["shares"].append({
            "name": "invalid", "volume": "backup", "directory": "invalid"
        }))
        check_modified(lambda d: d["deployment"].update(components=[
            "urbackup_client", "urbackup_server", "apps", "smb"
        ]))
        # Reordering independent applications is declarative, not hardcoded.
        rearranged = copy.deepcopy(p)
        rearranged["deployment"]["components"] = [
            "apps", "smb", "urbackup_server", "urbackup_client"
        ]
        (home / "provision.yaml").write_text(
            yaml.safe_dump(rearranged, allow_unicode=True), encoding="utf-8"
        )
        assert load_nas_provision(Path(td))["deployment"]["components"] == (
            rearranged["deployment"]["components"]
        )


    session = SimpleNamespace(host=SimpleNamespace(name="nas", address="192.168.1.12"))
    refuses(lambda: run_nas_phase(
        ROOT, session, "services", provision=p, policy={},
        verified_volumes=None, host_volumes=None,
    ))
    refuses(lambda: run_nas_phase(ROOT, session, "untrusted", provision=p, policy={}))


def test_role_and_no_application_specific_deploy_in_python() -> None:
    prefix = ROOT / "automation/ansible"
    entry = yaml.safe_load((prefix / "playbooks/deploy-nas.yml").read_text())
    assert entry[0]["roles"][0]["role"] == "nas"
    main = yaml.safe_load((prefix / "roles/nas/tasks/main.yml").read_text())
    assert any("include_tasks" in " ".join(task) for task in main)
    assert "nas_provision.deployment.components" in str(main)
    paths = [
        "base.yml", "urbackup_server.yml", "urbackup_client.yml", "apps.yml", "smb.yml",
    ]
    for name in paths:
        tasks = yaml.safe_load((prefix / "roles/nas/tasks" / name).read_text())
        assert isinstance(tasks, list) and len(tasks) > 4, name
    control = (
        ROOT / "scripts/infra-manager/infra_manager/physical_host_operations.py"
    ).read_text()
    assert "run_nas_phase(" in control
    assert "configure_nas_urbackup(" not in control
    assert "configure_nas_smb(" not in control
    assert control.index('run_nas_phase(\n                    repo_root, session, "base"') < (
        control.index("verified = verify_nas_volumes(")
    )
    assert control.index("verify_nas_backup_volumes(policy, verified)") < (
        control.index('run_nas_phase(\n                    repo_root, session, "services"')
    )


def test_shared_status_parser_and_secret_control() -> None:
    p = ROOT / "infrastructure/hosts/nas/status.yaml"
    checks = load_nas_status(ROOT, "nas", load_nas_provision(ROOT))
    assert checks == load_declared_checks(p, identity_field="host", identity_value="nas")
    assert len(checks) >= 5
    assert any(item.get("container") == "nas-urbackup-client" for item in checks)
    refuses(lambda: load_declared_checks(p, identity_field="host", identity_value="other"))
    refuses(lambda: load_declared_checks(p, identity_field="arbitrary", identity_value="nas"))
    reduced = copy.deepcopy(load_nas_provision(ROOT))
    reduced["deployment"]["components"].remove("apps")
    filtered = load_nas_status(ROOT, "nas", reduced)
    assert not any(x.get("component") == "apps" for x in filtered)
    assert any(x.get("component") == "base" for x in filtered)

    first = next(item for item in checks if item.get("container") == "nas-urbackup")
    calls = []
    def remote(_node, _address, args, *, capture_output=True):
        calls.append((tuple(args), capture_output))
        return SimpleNamespace(stdout="true\n", returncode=0)
    _run_declared_check("", "192.168.1.12", first, remote_executor=remote)
    assert calls and calls[0][0][:2] == ("docker", "inspect")

    service = {
        "name": "UrBackup",
        "url": "http://127.0.0.1:55414/",
        "operator": {"username": "admin", "password": {
            "source": "host_file",
            "path": "/etc/nas-apps/urbackup/admin-password",
        }},
    }
    def secret(_spec):
        raise AssertionError("Secrets must not be read in an untrusted status")
    fields = _service_fields(
        "", "192.168.1.12", service, show_secrets=False,
        password_resolver=secret,
    )
    assert ("Пароль", "скрыт") in fields
    trusted = _service_fields(
        "", "192.168.1.12", service, show_secrets=True,
        password_resolver=lambda spec: "dummy-credential",
    )
    assert ("Пароль", "dummy-credential") in trusted
    assert ("Логин", "admin") in trusted


if __name__ == "__main__":
    test_provision_and_fail_closed_phases()
    test_role_and_no_application_specific_deploy_in_python()
    test_shared_status_parser_and_secret_control()
    print("[ОК] NAS declarative deploy/status/credentials")
