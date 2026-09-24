#!/usr/bin/env python3
"""Unit and contract checks for infra-manager guest deployment."""

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

from infra_manager import guest_deploy as guest_deploy_module  # noqa: E402
from infra_manager.common import InfraManagerError  # noqa: E402
from infra_manager.guest_deploy import (  # noqa: E402
    DeploymentContext,
    DeploymentPaths,
    _apply_plan,
    _build_guest_plan,
    _find_guest_directory,
    _reconcile_guest_infrastructure,
    _validate_pve_and_state,
)
from infra_manager.opentofu import OpenTofuWorkspace  # noqa: E402


def fail(message: str) -> None:
    raise SystemExit(message)


def main_test() -> None:
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
    if _validate_pve_and_state(
        deployment,
        state_present=True,
        state_status="ready",
    ) is not None:
        fail("VM state validation must return None on success")

    invalid_state_cases = (
        (
            SimpleNamespace(
                find_vm=lambda vmid: {"type": "lxc", "name": "test-vm"}
            ),
            True,
            "ready",
            "VMID occupied by an LXC",
        ),
        (deployment_client, False, "", "VM without OpenTofu state"),
        (SimpleNamespace(find_vm=lambda vmid: None), True, "ready", "state without VM"),
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
            fail(f"VM state validation accepted an unsafe case: {description}")

    class TaintedClient:
        def __init__(self) -> None:
            self.resource = {"type": "qemu", "name": "test-vm"}
            self.tasks: list[tuple[str, str]] = []

        def find_vm(self, vmid: int):
            return self.resource

        def vm_status(self, *, node: str, vmid: int) -> str:
            return "stopped"

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
        fail("Tainted VM was not removed after type and name validation")
    if not any(command[2:4] == ["state", "rm"] for command in tainted_commands):
        fail("Tainted VM was not removed from OpenTofu state")

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
            fail("Target plan allowed an unrelated resource change")

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
        fail("Target plan parsed the selected VM actions incorrectly")

    class ProtectedTemplateClient:
        def __init__(self) -> None:
            self.protection_values: list[int] = []

        def vm_config(self, *, node: str, vmid: int) -> dict[str, int]:
            return {"template": 1, "protection": 1}

        def put(self, path: str, *, form: dict[str, int]) -> None:
            self.protection_values.append(form["protection"])

    protected_client = ProtectedTemplateClient()

    def failed_apply(argv: list[str], **kwargs: object):
        raise InfraManagerError("expected apply failure")

    with patch.object(guest_deploy_module, "run", failed_apply):
        try:
            _apply_plan(deployment_for(protected_client), ["create"])
        except InfraManagerError:
            pass
        else:
            fail("Expected tofu apply to fail")
    if protected_client.protection_values != [0, 1]:
        fail("Template protection was not restored after tofu apply failure")

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
            side_effect=InfraManagerError("expected plan failure"),
        ):
            try:
                _reconcile_guest_infrastructure(
                    deployment_for(deployment_client, paths=temporary_paths)
                )
            except InfraManagerError:
                pass
            else:
                fail("Expected OpenTofu plan construction to fail")
        if plan_file.exists():
            fail("Temporary OpenTofu plan was not removed after failure")

    guest_dir = _find_guest_directory(ROOT, 410)
    if guest_dir.name != "410-ai-control":
        fail(f"Unexpected directory found for VM 410: {guest_dir}")

    print("infra-manager guest deployment checks passed.")


if __name__ == "__main__":
    main_test()
