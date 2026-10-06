#!/usr/bin/env python3
"""Проверки единого состояния и операторского вывода гостей."""

from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = ROOT / "scripts" / "infra-manager"
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager import guest_status as status
from infra_manager.guest_catalog import guest_identity
from infra_manager.portal import portal_definitions


def fail(message: str) -> None:
    raise SystemExit(message)


def _resource(vmid: int, name: str, kind: str = "qemu") -> dict[str, object]:
    return {
        "vmid": vmid,
        "name": name,
        "type": kind,
        "node": "pve",
        "status": "running",
    }


def check_operator_contracts() -> None:
    expected = {
        109: {
            "AdGuard Home": (
                "admin",
                "guest_env",
                "/etc/network-gateway/adguard-admin.env",
                "ADGUARD_ADMIN_PASSWORD",
            ),
        },
        410: {
            "Open WebUI": (
                "admin@ai-control.local",
                "guest_env",
                "/etc/ai-control/open-webui.env",
                "WEBUI_ADMIN_PASSWORD",
            ),
        },
        910: {
            "Semaphore": (
                "admin",
                "guest_file",
                "/run/infra-manager/secrets/initial-admin-password",
                None,
            ),
            "OpenBao": (
                "operator",
                "pve_json",
                (
                    "/mnt/bindmounts/infra-manager/pve-only/"
                    "openbao/operator-access.json"
                ),
                "password",
            ),
        },
    }

    for vmid, services_expected in expected.items():
        identity = guest_identity(ROOT, vmid)
        services = portal_definitions(ROOT, identity)
        by_name = {service["name"]: service for service in services}
        if set(by_name) != set(services_expected):
            fail(
                f"Гость {vmid} имеет неверный набор операторских служб: "
                f"{sorted(by_name)}"
            )

        for name, contract in services_expected.items():
            operator = by_name[name].get("operator")
            if not isinstance(operator, dict):
                fail(f"{vmid} {name}: отсутствует operator-контракт")
            username, source, path, key = contract
            if operator.get("username") != username:
                fail(f"{vmid} {name}: неверный логин")
            password = operator.get("password")
            if not isinstance(password, dict):
                fail(f"{vmid} {name}: отсутствует источник пароля")
            expected_password = {"source": source, "path": path}
            if key is not None:
                expected_password["key"] = key
            if password != expected_password:
                fail(
                    f"{vmid} {name}: неверный источник пароля {password!r}"
                )


def check_status_definitions() -> None:
    expected_types = {
        109: ("docker", "systemd", "file"),
        311: (),
        410: ("docker", "docker", "file"),
        910: ("local_status",),
    }
    for vmid, types in expected_types.items():
        checks = status._load_common_checks(guest_identity(ROOT, vmid))
        actual = tuple(check["type"] for check in checks)
        if actual != types:
            fail(
                f"status.yaml гостя {vmid} имеет неверные проверки: {actual!r}"
            )


def check_declared_check_dispatch() -> None:
    with patch.object(
        status,
        "_run_guest_command",
        return_value=SimpleNamespace(stdout="true\n"),
    ) as run_guest:
        status._run_declared_check(
            "pve",
            "192.168.9.10",
            {
                "type": "local_status",
                "command": "/usr/local/sbin/infra-manager-status",
                "message": "ok",
            },
        )
    run_guest.assert_called_once_with(
        "pve",
        "192.168.9.10",
        [
            "/usr/local/sbin/infra-manager-status",
            "--full",
            "--quiet",
        ],
    )

    with patch.object(
        status,
        "_run_guest_command",
        return_value=SimpleNamespace(stdout="true\n"),
    ) as run_guest:
        status._run_declared_check(
            "pve",
            "192.168.4.10",
            {
                "type": "docker",
                "container": "open-webui",
                "message": "ok",
            },
        )
    args = run_guest.call_args.args
    if args[:2] != ("pve", "192.168.4.10"):
        fail("docker-проверка использует неверную цель")
    if args[2][-1] != "open-webui":
        fail("docker-проверка использует неверный контейнер")


def check_rendered_status() -> None:
    guest_files = {
        "/etc/network-gateway/adguard-admin.env": (
            "ADGUARD_ADMIN_PASSWORD=adguard-password\n"
        ),
        "/etc/ai-control/open-webui.env": (
            "WEBUI_ADMIN_PASSWORD=webui-password\n"
        ),
        "/run/infra-manager/secrets/initial-admin-password": (
            "semaphore-password\n"
        ),
    }

    def read_guest_file(node: str, address: str, path: str) -> str:
        if node != "pve" or not address.startswith("192.168."):
            raise AssertionError("Единый статус использует неверную цель")
        return guest_files[path]

    def read_pve_json(node: str, path: str, key: str) -> str:
        if (
            node != "pve"
            or path
            != "/mnt/bindmounts/infra-manager/pve-only/openbao/operator-access.json"
            or key != "password"
        ):
            raise AssertionError("Неожиданный pve_json")
        return "openbao-password"

    cases = (
        (
            109,
            "network-gateway",
            "qemu",
            "192.168.1.9",
            ("AdGuard Home", "adguard-password"),
        ),
        (
            410,
            "ai-control",
            "qemu",
            "192.168.4.10",
            ("Open WebUI", "webui-password"),
        ),
        (
            910,
            "infra-manager",
            "lxc",
            "192.168.9.10",
            ("Semaphore", "semaphore-password", "OpenBao", "openbao-password"),
        ),
    )

    with (
        patch.object(status, "_read_guest_file", side_effect=read_guest_file),
        patch.object(status, "read_pve_json_value", side_effect=read_pve_json),
    ):
        for vmid, name, kind, address, expected_values in cases:
            identity = guest_identity(ROOT, vmid)
            services = tuple(portal_definitions(ROOT, identity))
            with patch.object(
                status,
                "verify_guest_status",
                return_value=("pve", address, services, ("SSH доступен",)),
            ):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    status.show_guest_status(
                        ROOT,
                        identity,
                        _resource(vmid, name, kind),
                        full=True,
                        show_secrets=True,
                    )
            text = output.getvalue()
            for expected in (
                f"Гость {vmid} — {name}",
                *expected_values,
            ):
                if expected not in text:
                    fail(
                        f"Единый статус {vmid} не содержит {expected!r}:\n{text}"
                    )


def check_secret_modes() -> None:
    identity = guest_identity(ROOT, 410)
    services = tuple(portal_definitions(ROOT, identity))
    resource = _resource(410, "ai-control")
    verified = ("pve", "192.168.4.10", services, ("SSH доступен",))

    with (
        patch.object(status, "verify_guest_status", return_value=verified),
        patch.object(
            status,
            "_resolve_password",
            side_effect=AssertionError("Пароль не должен читаться"),
        ),
    ):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status.show_guest_status(
                ROOT,
                identity,
                resource,
                full=True,
                show_secrets=False,
            )
        if "Пароль:" not in output.getvalue() or "скрыт" not in output.getvalue():
            fail("Скрытый режим должен явно скрывать пароль")

        status.show_guest_status(
            ROOT,
            identity,
            resource,
            full=False,
            show_secrets=False,
        )


def main() -> None:
    check_operator_contracts()
    check_status_definitions()
    check_declared_check_dispatch()
    check_rendered_status()
    check_secret_modes()
    print("[ОК] Единый статус гостей проверен")


if __name__ == "__main__":
    main()
