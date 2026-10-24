#!/usr/bin/env python3
"""Offline tests of declarative NAS deploy/status. No SSH or disk changes."""
from __future__ import annotations

import copy
import contextlib
import io
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/infra-manager"))

from infra_manager.common import InfraManagerError
from infra_manager.physical_nas_status import (
    load_nas_status, _load_nas_status_document, show_nas_status,
)
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
        schema_dir = Path(td) / "infrastructure/schemas"
        schema_dir.mkdir(parents=True)
        (schema_dir / "physical-nas-provision.schema.yaml").write_text(
            (ROOT / "infrastructure/schemas/physical-nas-provision.schema.yaml").read_text(),
            encoding="utf-8",
        )
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
        check_modified(lambda d: d["services"]["qbittorrent"]["runtime"].update(
            torrent_port=22
        ))
        check_modified(lambda d: d["services"]["qbittorrent"]["runtime"].update(
            directories=["../etc"]
        ))
        check_modified(lambda d: d["services"]["qbittorrent"]["runtime"].update(
            timezone="../bad;command"
        ))
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
        check_modified(lambda d: d["deployment"]["dependencies"].update(
            urbackup_client=[]
        ))
        check_modified(lambda d: d["deployment"]["dependencies"].update(
            apps=["smb"]
        ))
        check_modified(lambda d: d["deployment"]["dependencies"].update(
            urbackup_server=["urbackup_client"]
        ))
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
        # Optional apps may be excluded along with the entire qBittorrent service.
        reduced = copy.deepcopy(p)
        reduced["deployment"]["components"] = [
            "urbackup_server", "urbackup_client", "smb",
        ]
        del reduced["services"]["qbittorrent"]
        (home / "provision.yaml").write_text(
            yaml.safe_dump(reduced, allow_unicode=True), encoding="utf-8",
        )
        assert "qbittorrent" not in load_nas_provision(Path(td))["services"]


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
    assertions = main[0]["ansible.builtin.assert"]["that"]
    assert any(
        "'apps' not in nas_provision.deployment.components" in item
        and "services.get('qbittorrent'" in item
        for item in assertions
    ), "Disabled apps must not require a qBittorrent service"
    assert not any(
        "components == ['urbackup_server'" in item for item in assertions
    ), "Component composition must not be hardcoded"
    assert any("difference(" in item for item in assertions)
    assert "subelements('value')" in str(main), "Declared dependencies must be enforced"
    app_tasks = (prefix / "roles/nas/tasks/apps.yml").read_text()
    assert "nas_provision.services.qbittorrent.runtime.directories" in app_tasks
    compose = (prefix / "roles/nas/templates/nas-qbittorrent.compose.yml.j2").read_text()
    for key in ("timezone", "web_port", "torrent_port", "downloads_directory"):
        assert "nas_provision.services.qbittorrent.runtime." + key in compose
    paths = [
        "base.yml", "urbackup_server.yml", "urbackup_client.yml", "apps.yml", "smb.yml",
    ]
    for name in paths:
        tasks = yaml.safe_load((prefix / "roles/nas/tasks" / name).read_text())
        assert isinstance(tasks, list) and len(tasks) > 4, name
    control = (
        ROOT / "scripts/infra-manager/drivers/omv8.py"
    ).read_text()
    assert "run_nas_phase(" in control
    assert "configure_nas_urbackup(" not in control
    assert "configure_nas_smb(" not in control
    core = (ROOT / "scripts/infra-manager/infra_manager/physical_host_operations.py").read_text()
    assert "run_nas_phase" not in core
    assert "physical_nas" not in core
    assert control.index('run_nas_phase(\n                    repo_root, session, "base"') < (
        control.index("verified = verify_nas_volumes(")
    )
    assert control.index("verify_nas_backup_volumes(\n") < (
        control.index('run_nas_phase(\n                    repo_root, session, "services"')
    )


def test_shared_status_parser_and_secret_control() -> None:
    p = ROOT / "infrastructure/hosts/nas/status.yaml"
    checks = load_nas_status(ROOT, "nas", load_nas_provision(ROOT))
    assert checks == load_declared_checks(p, identity_field="host", identity_value="nas")
    assert len(checks) >= 5
    assert _load_nas_status_document(ROOT, "nas")["health"]["disk_usage_warning_percent"] == 95
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


def test_nas_smb_password_only_in_trusted_status() -> None:
    """The shared password is mandatory in trusted status, hidden elsewhere."""
    config = copy.deepcopy(load_nas_provision(ROOT))
    config["services"] = {}
    session = SimpleNamespace(host=SimpleNamespace(name="nas", address="192.0.2.12"))
    with (
        patch("infra_manager.physical_nas_status.project_branch_for_checkout", return_value="main"),
        patch("infra_manager.physical_nas_status.current_project_revision", return_value="test"),
        patch("infra_manager.physical_host_credentials.read_operator_secret",
              return_value="unit-test-secret") as read_secret,
    ):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            show_nas_status(ROOT, session, config, show_secrets=False)
        assert "Samba (SMB)" in output.getvalue()
        assert "sergey, sveta, danya" in output.getvalue()
        assert "скрыт" in output.getvalue()
        assert "unit-test-secret" not in output.getvalue()
        read_secret.assert_not_called()

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            show_nas_status(ROOT, session, config, show_secrets=True)
        assert "unit-test-secret" in output.getvalue()
        assert "shared" in output.getvalue()
        assert "movies" in output.getvalue()
        assert "music" in output.getvalue()
        assert "data" in output.getvalue()
        read_secret.assert_called_once()


def test_generic_physical_host_boundary() -> None:
    """Nothing in the physical-host controller depends on specific services."""
    core = ROOT / "scripts/infra-manager/infra_manager"
    for name in (
        "operator.py", "physical_hosts.py", "physical_host_operations.py",
        "physical_host_credentials.py",
    ):
        content = (core / name).read_text(encoding="utf-8").lower()
        for forbidden in ("smb-password-set", "physical_nas_", "run_nas_phase", "omv8"):
            assert forbidden not in content, (name, forbidden)
    thin_host = (ROOT / "scripts/infra-manager/host/manager.py").read_text()
    assert "prompt_nas_smb_password" not in thin_host
    assert "prompt_physical_credentials" not in thin_host
    assert "prepare_physical_host" in thin_host
    assert (ROOT / "scripts/infra-manager/drivers/omv8.py").is_file()
    manifest = yaml.safe_load((ROOT / "infrastructure/hosts/nas/operator.yaml").read_text())
    assert manifest["credentials"][0]["id"] == "smb"
    assert manifest["credentials"][0]["status"]["title"] == "Samba (SMB)"


if __name__ == "__main__":
    test_provision_and_fail_closed_phases()
    test_role_and_no_application_specific_deploy_in_python()
    test_shared_status_parser_and_secret_control()
    test_nas_smb_password_only_in_trusted_status()
    test_generic_physical_host_boundary()
    print("[ОК] NAS declarative deploy/status/credentials")
