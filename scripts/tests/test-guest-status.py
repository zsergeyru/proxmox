#!/usr/bin/env python3
"""Проверки единого операторского статуса гостей."""

from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path
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
        109: (
            "AdGuard Home",
            "admin",
            "guest_env",
            "/etc/network-gateway/adguard-admin.env",
            "ADGUARD_ADMIN_PASSWORD",
        ),
        410: (
            "Open WebUI",
            "admin@ai-control.local",
            "guest_env",
            "/etc/ai-control/open-webui.env",
            "WEBUI_ADMIN_PASSWORD",
        ),
    }

    for vmid, contract in expected.items():
        identity = guest_identity(ROOT, vmid)
        services = portal_definitions(ROOT, identity)
        if len(services) != 1:
            fail(f"Гость {vmid} должен иметь одну операторскую службу")
        service = services[0]
        name, username, source, path, key = contract
        operator = service.get("operator")
        if service.get("name") != name or not isinstance(operator, dict):
            fail(f"Гость {vmid} имеет неверный operator-контракт")
        if operator.get("username") != username:
            fail(f"Гость {vmid} имеет неверный логин")
        password = operator.get("password")
        if not isinstance(password, dict):
            fail(f"Гость {vmid} не имеет источника пароля")
        if password != {
            "source": source,
            "path": path,
            "key": key,
        }:
            fail(f"Гость {vmid} имеет неверный источник пароля: {password!r}")

    infra = guest_identity(ROOT, 910)
    services = portal_definitions(ROOT, infra)
    by_name = {service["name"]: service for service in services}
    if set(by_name) != {"Semaphore", "OpenBao"}:
        fail(f"910 должен публиковать Semaphore и OpenBao: {sorted(by_name)}")

    semaphore = by_name["Semaphore"]["operator"]
    if semaphore != {
        "username": "admin",
        "password": {
            "source": "guest_file",
            "path": "/run/infra-manager/secrets/initial-admin-password",
        },
    }:
        fail("Semaphore имеет неверный operator-контракт")

    openbao = by_name["OpenBao"]["operator"]
    if openbao != {
        "username": "operator",
        "password": {
            "source": "pve_json",
            "path": (
                "/mnt/bindmounts/infra-manager/pve-only/"
                "openbao/operator-access.json"
            ),
            "key": "password",
        },
    }:
        fail("OpenBao имеет неверный operator-контракт")


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
        if node != "pve":
            fail(f"Неожиданный PVE node: {node}")
        if not address.startswith("192.168."):
            fail(f"Неожиданный адрес гостя: {address}")
        try:
            return guest_files[path]
        except KeyError as exc:
            raise AssertionError(f"Неожиданный guest_file: {path}") from exc

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
        (109, "network-gateway", "qemu", "AdGuard Home", "adguard-password"),
        (410, "ai-control", "qemu", "Open WebUI", "webui-password"),
        (910, "infra-manager", "lxc", "Semaphore", "semaphore-password"),
    )

    with (
        patch.object(status, "_read_guest_file", side_effect=read_guest_file),
        patch.object(status, "read_pve_json_value", side_effect=read_pve_json),
    ):
        for vmid, name, kind, service_name, password in cases:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status.show_guest_status(
                    ROOT,
                    guest_identity(ROOT, vmid),
                    _resource(vmid, name, kind),
                )
            text = output.getvalue()
            for expected in (
                f"Гость {vmid} {name}",
                "Объект PVE запущен",
                service_name,
                password,
            ):
                if expected not in text:
                    fail(
                        f"Единый статус {vmid} не содержит {expected!r}:\n{text}"
                    )

            if vmid == 109 and "http://192.168.1.9:3000/" not in text:
                fail("109 имеет неверный URL AdGuard Home")
            if vmid == 410 and "http://192.168.4.10:4100/" not in text:
                fail("410 имеет неверный URL Open WebUI")
            if vmid == 910:
                for expected in (
                    "http://192.168.9.10:3000/",
                    "https://192.168.9.10:8202/ui/",
                    "openbao-password",
                ):
                    if expected not in text:
                        fail(f"910 не содержит {expected!r}")


def check_env_reader() -> None:
    if status._read_env_value(
        "A=1\nPASSWORD=value-with-=\n",
        "PASSWORD",
        "/tmp/test.env",
    ) != "value-with-=":
        fail("ENV-источник искажает значение после первого '='")

    try:
        status._read_env_value(
            "PASSWORD=one\nPASSWORD=two\n",
            "PASSWORD",
            "/tmp/test.env",
        )
    except Exception:
        pass
    else:
        fail("ENV-источник должен отклонять повторяющийся ключ")


def main() -> None:
    check_operator_contracts()
    check_rendered_status()
    check_env_reader()
    print("[ОК] Единый операторский статус гостей проверен")


if __name__ == "__main__":
    main()
