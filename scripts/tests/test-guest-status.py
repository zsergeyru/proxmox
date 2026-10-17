#!/usr/bin/env python3
"""Проверки единого состояния и операторского вывода гостей."""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = ROOT / "scripts" / "infra-manager"
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager import guest_status as status
from infra_manager.common import CommandError, Console, InfraManagerError
from infra_manager.guest_catalog import guest_identity
from infra_manager.portal import operator_service_definitions, portal_definitions


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
            "Hermes Dashboard": (
                "admin",
                "guest_env",
                "/etc/ai-control/hermes.env",
                "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD",
            ),
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


def check_operator_services_140() -> None:
    """Для 140 показывать адреса и защищённый пароль без Homepage."""
    identity = guest_identity(ROOT, 140)
    services = operator_service_definitions(ROOT, identity)
    if [x["name"] for x in services] != ["Authelia", "Домашний сервер"]:
        fail("140: нужны Authelia и домашний сервер в общем статусе")
    authelia, portal = services
    if authelia["url"] != "https://auth.zsergey.ru":
        fail("140: неверный адрес Authelia")
    if authelia.get("setup_url") != "https://auth.zsergey.ru/totp-setup":
        fail("140: ссылка первоначальной настройки TOTP должна быть в статусе")
    if portal.get("setup_url") is not None or portal.get("connection_key") is not None:
        fail("140: веб-портал не должен требовать ручной ссылки и ключа")
    steps = authelia.get("setup_instructions")
    if not isinstance(steps, list) or len(steps) != 4:
        fail("140: нужны четыре понятных шага подключения телефона")
    if any("TOTP" in step or "QR-код" in step for step in steps):
        fail("140: в шагах для пользователя не должно быть специальных терминов")
    if "Google Authenticator" not in steps[1] or "шесть цифр" not in steps[2]:
        fail("140: описать добавление телефона и ввод шести цифр")
    if authelia["operator"] != {
        "username": "admin",
        "password": {
            "source": "guest_credential",
            "path": "/var/lib/web-gateway/management/operator-admin.json",
            "users_file": "/var/lib/web-gateway/authelia/users_database.yml",
        },
    }:
        fail("140: неверный источник операторского пароля")
    if portal["url"] != "https://home.zsergey.ru":
        fail("140: неверный адрес веб-портала")
    if portal["authentication"] != "Authelia":
        fail("140: вход на домашнюю страницу должен использовать Authelia")
    if portal_definitions(ROOT, identity):
        fail("140: веб-страница не должна создавать второй портал Homepage")


def check_credential_validation_140() -> None:
    """Не показывать старый пароль после изменения хеша Authelia."""
    services = operator_service_definitions(ROOT, guest_identity(ROOT, 140))
    spec = services[0]["operator"]["password"]
    credential = json.dumps({
        "username": "admin",
        "password": "initial-authelia-secret",
        "password_hash": "$argon2id$oldhash",
    })
    def reader(node: str, address: str, path: str) -> str:
        if path == spec["path"]:
            return credential
        if path == spec["users_file"]:
            return 'users:\n  admin:\n    password: "$argon2id$oldhash"\n'
        raise AssertionError(path)

    with patch.object(status, "_read_guest_file", side_effect=reader):
        if status._resolve_password("pve", "192.168.1.40", spec) != "initial-authelia-secret":
            fail("140: статус не вернул сохранённый действующий пароль")

    with patch.object(status, "_read_guest_file", side_effect=lambda n, a, p: (
        credential if p == spec["path"] else 'users:\n  admin:\n    password: "$argon2id$newhash"\n'
    )):
        actual = status._resolve_password("pve", "192.168.1.40", spec)
        if "initial-authelia-secret" in actual or "изменён" not in actual:
            fail("140: нельзя показывать пароль после изменения хеша")

    with patch.object(status, "_read_guest_file", side_effect=CommandError(["cat"], 1)):
        actual = status._resolve_password("pve", "192.168.1.40", spec)
        if "не сохранён" not in actual:
            fail("140: отсутствие прежнего пароля не должно ломать статус")

    with (
        patch.object(status, "verify_guest_status", return_value=(
            "pve", "192.168.1.40", tuple(services), ("SSH доступен",)
        )),
        patch.object(status, "_read_guest_file", side_effect=AssertionError("Пароль прочитан в Semaphore")),
    ):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status.show_guest_status(
                ROOT, guest_identity(ROOT, 140), _resource(140, "web-gateway", "lxc"),
                show_secrets=False,
            )
        text = output.getvalue()
        for expected in ("https://auth.zsergey.ru", "https://auth.zsergey.ru/totp-setup", "Первое подключение телефона", "Шаг 1:", "Шаг 2:", "Шаг 3:", "Шаг 4:", "шесть цифр", "https://home.zsergey.ru", "Логин:", "скрыт", "Authelia"):
            if expected not in text:
                fail(f"140: в скрытом статусе отсутствует {expected}")


def check_immich_accounts() -> None:
    """Проверить описание трёх учётных записей Immich."""
    services = portal_definitions(ROOT, guest_identity(ROOT, 340))
    immich = next(service for service in services if service["name"] == "Immich")
    operator = immich.get("operator")
    emails = (
        "admin@zsergey.ru",
        "mail@zsergey.ru",
        "zsveta@elsoft35.ru",
    )
    accounts = operator.get("accounts") if isinstance(operator, dict) else None
    if not isinstance(accounts, list) or len(accounts) != 3:
        fail("340: требуется ровно три операторские учётные записи")
    for account, email in zip(accounts, emails, strict=True):
        if account != {
            "username": email,
            "password": {
                "source": "guest_json",
                "path": "/mnt/persistent-state/secrets/immich-initial-users.json",
                "key": email,
            },
        }:
            fail(f"340: неверное описание учётной записи {email}")


def check_status_definitions() -> None:
    expected_types = {
        109: ("docker", "systemd", "file"),
        311: (),
        340: (
            "docker", "docker", "docker", "http", "http",
            "docker", "http", "docker_image", "docker_image",
        ),
        410: ("docker", "docker", "http", "file"),
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
            project_branch="feature/ephemeral-guest-ssh",
        )
    run_guest.assert_called_once_with(
        "pve",
        "192.168.9.10",
        [
            "env",
            "INFRA_PROJECT_BRANCH=feature/ephemeral-guest-ssh",
            "/usr/local/sbin/infra-manager-status",
            "--full",
            "--quiet",
        ],
    )

    with (
        patch.object(status, "_run_guest_command", return_value=SimpleNamespace(
            stdout="ignored output\n[ПРЕДУПРЕЖДЕНИЕ] В Semaphore отсутствует гость 340\n",
        )),
        patch.object(status, "console") as output,
    ):
        status._run_declared_check(
            "pve", "192.168.9.10",
            {"type": "local_status", "command": "/usr/local/sbin/infra-manager-status"},
        )
    output.warning.assert_called_once_with("В Semaphore отсутствует гость 340")

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


def check_docker_image_status() -> None:
    """Наличие образа не выдавать за запуск выключенного дополнения."""
    image = "ghcr.io/immich-power-tools/immich-power-tools:latest"
    with patch.object(
        status,
        "_run_guest_command",
        return_value=SimpleNamespace(stdout="sha256:example"),
    ) as run_guest:
        status._run_declared_check(
            "pve",
            "192.168.3.40",
            {
                "type": "docker_image",
                "image": image,
                "message": "Образ загружен",
            },
        )
    run_guest.assert_called_once_with(
        "pve",
        "192.168.3.40",
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
    )

    try:
        status._run_declared_check(
            "pve", "192.168.3.40",
            {"type": "docker_image", "image": "", "message": "Ошибка"},
        )
    except InfraManagerError as exc:
        if "image" not in str(exc):
            fail("Нет объяснения при пустом имени образа")
    else:
        fail("Пустой образ должен отклоняться")


def check_dashboard_port() -> None:
    identity = guest_identity(ROOT, 109)
    resource = _resource(109, "network-gateway")

    with (
        patch.object(status, "_check_tcp") as check_tcp,
        patch.object(status, "_run_guest_command") as run_guest,
        patch.object(status, "_run_declared_check"),
    ):
        node, address, _, messages = status.verify_guest_status(
            ROOT,
            identity,
            resource,
        )

    if node != "pve" or address != "192.168.1.9":
        fail("Базовый статус вернул неверную цель для панели")
    tcp_targets = [call.args for call in check_tcp.call_args_list]
    for expected in (
        ("192.168.1.9", 22),
        ("192.168.1.9", 3001),
        ("192.168.1.9", 3000),
    ):
        if expected not in tcp_targets:
            fail(f"Базовый статус не проверяет TCP {expected!r}")
    run_guest.assert_called_once_with("pve", "192.168.1.9", ["true"])
    if "SSH по сертификату доступен" not in messages:
        fail("Базовый статус не проверил сертификатный SSH-вход")
    if "Локальная панель доступна" not in messages:
        fail("Базовый статус не сообщает о готовности локальной панели")


def check_rendered_status() -> None:
    guest_files = {
        "/etc/network-gateway/adguard-admin.env": (
            "ADGUARD_ADMIN_PASSWORD=adguard-password\n"
        ),
        "/etc/ai-control/open-webui.env": (
            "WEBUI_ADMIN_PASSWORD=webui-password\n"
        ),
        "/etc/ai-control/hermes.env": (
            "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=dashboard-password\n"
        ),
        "/run/infra-manager/secrets/initial-admin-password": (
            "semaphore-password\n"
        ),
        "/mnt/persistent-state/secrets/immich-initial-users.json": (
            '{"admin@zsergey.ru": "immich-admin-password", '
            '"mail@zsergey.ru": "immich-user-password", '
            '"zsveta@elsoft35.ru": "immich-family-password"}'
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
            (
                "http://192.168.1.9:3001/",
                "AdGuard Home",
                "adguard-password",
            ),
        ),
        (
            340,
            "photo-archive",
            "lxc",
            "192.168.3.40",
            (
                "http://192.168.3.40:3001/",
                "Immich",
                "http://192.168.3.40:2283/",
                "admin@zsergey.ru",
                "immich-admin-password",
                "mail@zsergey.ru",
                "immich-user-password",
                "zsveta@elsoft35.ru",
                "immich-family-password",
            ),
        ),
        (
            410,
            "ai-control",
            "qemu",
            "192.168.4.10",
            (
                "http://192.168.4.10:3001/",
                "Open WebUI",
                "webui-password",
                "Hermes Dashboard",
                "dashboard-password",
            ),
        ),
        (
            910,
            "infra-manager",
            "lxc",
            "192.168.9.10",
            (
                "http://192.168.9.10:3001/",
                "Semaphore",
                "semaphore-password",
                "OpenBao",
                "openbao-password",
            ),
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
                        project_branch="feature/unified-guest-status",
                        project_revision="abc1234",
                    )
            text = output.getvalue()
            for expected in (
                f"Гость {vmid} — {name}",
                "Проект",
                "feature/unified-guest-status",
                "abc1234",
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

    with (
        patch.object(status, "verify_guest_status", return_value=verified),
        patch.object(
            status,
            "_resolve_password",
            side_effect=AssertionError("Пароль не должен читаться по умолчанию"),
        ),
    ):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status.show_guest_status(
                ROOT,
                identity,
                resource,
                full=True,
            )
        if "Пароль:" not in output.getvalue() or "скрыт" not in output.getvalue():
            fail("Режим по умолчанию должен скрывать пароль")


def check_immich_hidden_passwords() -> None:
    """В режиме Semaphore не читать ни один пароль Immich."""
    identity = guest_identity(ROOT, 340)
    services = tuple(portal_definitions(ROOT, identity))
    verified = ("pve", "192.168.3.40", services, ("SSH доступен",))
    with (
        patch.object(status, "verify_guest_status", return_value=verified),
        patch.object(
            status,
            "_read_guest_file",
            side_effect=AssertionError("Semaphore не должен читать пароли Immich"),
        ),
    ):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status.show_guest_status(
                ROOT,
                identity,
                _resource(340, "photo-archive", "lxc"),
                full=True,
                show_secrets=False,
            )
        result = output.getvalue()
        if result.count("скрыт") != 3 or "immich-admin-password" in result:
            fail("340: три пароля должны быть скрыты в Semaphore")


def check_ansible_error_classification() -> None:
    """Нулевой unreachable в сводке Ansible не означает сетевую ошибку."""
    output = io.StringIO()
    error = CommandError(
        ["ansible-playbook"], 2,
        "PLAY RECAP: guest : ok=9 changed=0 unreachable=0 failed=1",
    )
    Console(err=output).failure(error, vmid=340)
    result = output.getvalue()
    if "Не удалось подключиться" in result or "infra-manager status 340" not in result:
        fail("Сбой Ansible неправильно классифицирован как недоступность сети")


    # Задание показываем, а сырой fatal/stdout с секретами — никогда.
    output = io.StringIO()
    error = CommandError(
        ["ansible-playbook"], 2,
        (
            "TASK [photo_archive : Создать администратора и семейную библиотеку]\n"
            'fatal: [340]: FAILED! => {"stderr": "ОШИБКА первоначальной настройки '
            'Immich: Истекло время ожидания команды Immich для администратора", '
            '"password": "VERY-SECRET-PASSWORD"}\n'
            "PLAY RECAP: 340 : ok=29 unreachable=0 failed=1"
        ),
    )
    Console(err=output).failure(error, vmid=340)
    result = output.getvalue()
    for required in (
        "photo_archive : Создать администратора и семейную библиотеку",
        "не ответил на запрос сброса пароля",
        "infra-manager status 340",
    ):
        if required not in result:
            fail(f"Нет безопасной диагностики Ansible: {required}")
    if "VERY-SECRET-PASSWORD" in result or "fatal:" in result:
        fail("Диагностика Ansible раскрыла сырой вывод или пароль")

    # Другая ошибка Ansible: только имя задания, без неразрешённых подробностей.
    output = io.StringIO()
    error = CommandError(
        ["ansible-playbook"], 2,
        (
            "TASK [Проверить службу]\n"
            'fatal: [340]: FAILED! => {"stdout": "SECRET-TOKEN-123"}\n'
            "PLAY RECAP: 340 : unreachable=0 failed=1"
        ),
    )
    Console(err=output).failure(error, vmid=340)
    result = output.getvalue()
    if "Проверить службу" not in result or "SECRET-TOKEN-123" in result:
        fail("Небезопасная диагностика неизвестной ошибки Ansible")


def main() -> None:
    check_operator_contracts()
    check_operator_services_140()
    check_credential_validation_140()
    check_immich_accounts()
    check_status_definitions()
    check_declared_check_dispatch()
    check_docker_image_status()
    check_dashboard_port()
    check_rendered_status()
    check_secret_modes()
    check_immich_hidden_passwords()
    check_ansible_error_classification()
    print("[ОК] Единый статус гостей проверен")


if __name__ == "__main__":
    main()
