#!/usr/bin/env python3
"""Проверить машинные файлы и шаблоны веб-шлюза 140 без Docker и секретов."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined


ROOT = Path(__file__).resolve().parents[2]
GUEST_DIR = ROOT / "infrastructure/guests/140-web-gateway"
ROLE = ROOT / "automation/ansible/roles/web_gateway"
PLAYBOOK = ROOT / "automation/ansible/playbooks/configure-guest.yml"


def require(condition: bool, message: str) -> None:
    """Остановить проверку при нарушении контракта."""
    if not condition:
        raise AssertionError(message)


def load(path: Path) -> dict:
    """Загрузить YAML с отображением точного пути ошибки."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise AssertionError(f"{path}: {exc}") from exc
    require(isinstance(data, dict), f"{path}: ожидался YAML-объект")
    return data


def test() -> None:
    """Проверить маршруты, постоянное состояние, контейнеры и роль."""
    guest = load(GUEST_DIR / "guest.yaml")
    provision = load(GUEST_DIR / "provision.yaml")
    checks = load(GUEST_DIR / "status.yaml")
    storage = provision["pve_storage"]
    web = provision["web_gateway"]

    require(guest["vmid"] == provision["guest_vmid"] == checks["guest_vmid"] == 140,
            "VMID машинных описаний различается")
    require(guest["profile"] == "debian-lxc-docker", "140 должен быть LXC Docker")
    require("portal" not in provision, "140 использует Homarr, а не Homepage")
    require(storage["volumes"] == [{
        "name": "infra_state",
        "size_gb": 40,
        "mountpoint": "/mnt/bindmounts/infra-state",
    }], "Нельзя создавать новый том вместо общего infra_state")
    require(storage["mounts"] == [{
        "slot": "mp0",
        "volume": "infra_state",
        "directory": "140-web-gateway",
        "target": "/var/lib/web-gateway",
        "read_only": False,
    }], "Постоянное состояние должно иметь отдельное подключение mp0")
    require(web["storage"]["allow_missing_mount"] is False,
            "Нельзя запускать приложения без постоянного тома")
    require(web["paths"]["persistent_state"] in provision["persistence"]["backup_required"],
            "Не перечислен обязательный каталог резервирования")
    require(web["secrets"]["provider"] == "persistent-files",
            "Секреты гостя 140 не должны зависеть от OpenBao")
    require(web["paths"]["secrets"].startswith(web["paths"]["persistent_state"] + "/"),
            "Секреты обязаны лежать на постоянном mp0")
    require(web["authelia"]["users_file"].startswith(web["paths"]["persistent_state"] + "/"),
            "Файл пользователей обязан лежать на постоянном mp0")
    require(set(web["authelia"]["authentication"]["groups"]) == {"admins", "family", "guests"},
            "Нужны три согласованные группы доступа")
    require(set(web["authelia"]["oidc"]["clients"]) >= {"homarr", "immich"},
            "Нужны отдельные клиенты OIDC для Homarr и Immich")

    environment = Environment(
        loader=FileSystemLoader(str(ROLE / "templates")),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    environment.filters["bool"] = bool
    variables = {"provision": provision, "web_gateway_routes": web["caddy"]["routes"]}

    compose = yaml.safe_load(environment.get_template("docker-compose.yml.j2").render(**variables))
    local_caddy = environment.get_template("Caddyfile.j2").render(**variables)
    local_home = (ROLE / "files/local-home.html").read_text(encoding="utf-8")
    require(web["caddy"]["listen"]["local_http"] == 8080,
            "Локальная страница должна слушать согласованный порт 8080")
    require(":8080 {" in local_caddy and "file_server" in local_caddy,
            "Caddy должен отдавать локальную страницу по HTTP")
    require("remote_ip 192.168.0.0/16" in local_caddy
            and "respond" in local_caddy and "403" in local_caddy,
            "Вход по HTTP разрешён только из домашней сети")
    require("root * /etc/caddy/local-home" in local_caddy,
            "Локальная страница должна обслуживаться из отдельного каталога")
    require("<html lang=\"ru\">" in local_home and "Домашний сервер" in local_home,
            "Отсутствует готовая домашняя страница")
    require("/init" not in local_home and "http://" not in local_home,
            "Страница не должна отправлять на первичную настройку Homarr")
    services = compose["services"]
    require(f'{web["caddy"]["listen"]["local_http"]}:8080' in
            [str(port) for port in services["caddy"]["ports"]],
            "Docker обязан публиковать отдельный порт домашней страницы")
    require(all("7575" not in str(port) for port in services["caddy"]["ports"]),
            "Домашняя страница не должна открывать прямой порт Homarr")
    require(set(services) == {"caddy", "authelia", "homarr"},
            "Состав Docker Compose отличается от контракта")
    for name, expected in [
        ("caddy", web["caddy"]),
        ("authelia", web["authelia"]),
        ("homarr", web["homarr"]),
    ]:
        require(services[name]["image"] == expected["image"],
                f"Неверный образ {name}")
        require("latest" not in services[name]["image"],
                f"Образ {name} должен иметь закреплённую версию")
    require(not any(
        "/var/run/docker.sock" in volume
        for service in services.values()
        for volume in service.get("volumes", [])
    ), "Контейнеры 140 не должны получать Docker socket")
    require(all(
        "0.0.0.0:" not in str(port)
        for port in services["homarr"]["ports"]
    ), "Homarr нельзя публиковать в домашнюю сеть отдельным портом")
    require(any(
        str(port).startswith("127.0.0.1:")
        for port in services["homarr"]["ports"]
    ), "Для диагностики Homarr допускается только локальный порт")
    require(set(services["authelia"]["environment"]) >= {
        "AUTHELIA_SESSION_SECRET_FILE",
        "AUTHELIA_STORAGE_ENCRYPTION_KEY_FILE",
        "AUTHELIA_IDENTITY_PROVIDERS_OIDC_HMAC_SECRET_FILE",
        "AUTHELIA_IDENTITY_VALIDATION_RESET_PASSWORD_JWT_SECRET_FILE",
    }, "Authelia должна получать секреты из защищённых файлов")
    # Секреты нельзя подключать внутрь /config: он уже смонтирован только
    # для чтения и Docker не сможет создать в нём вложенную точку подключения.
    authelia_volumes = services["authelia"]["volumes"]
    config_mount = web["paths"]["configuration"] + "/authelia:/config:ro"
    secrets_container_path = "/run/authelia-secrets"
    secrets_mount = web["paths"]["secrets"] + ":" + secrets_container_path + ":ro"
    require(config_mount in authelia_volumes,
            "Конфигурация Authelia должна оставаться только для чтения")
    require(secrets_mount in authelia_volumes,
            "Секреты Authelia должны подключаться отдельным закрытым томом")
    require(not secrets_container_path.startswith("/config/"),
            "Нельзя подключать секреты внутрь /config:ro")
    secret_files = {
        "AUTHELIA_SESSION_SECRET_FILE": "authelia-session-secret",
        "AUTHELIA_STORAGE_ENCRYPTION_KEY_FILE": "authelia-storage-encryption-key",
        "AUTHELIA_IDENTITY_VALIDATION_RESET_PASSWORD_JWT_SECRET_FILE": "authelia-jwt-secret",
        "AUTHELIA_IDENTITY_PROVIDERS_OIDC_HMAC_SECRET_FILE": "authelia-oidc-hmac-secret",
    }
    for key, filename in secret_files.items():
        require(
            services["authelia"]["environment"].get(key)
            == secrets_container_path + "/" + filename,
            f"Authelia: {key} должен ссылаться на подключённый каталог",
        )
    require(services["homarr"]["env_file"] == [web["paths"]["secrets"] + "/homarr.env"],
            "Homarr должен использовать защищённый файл параметров")
    require(services["homarr"]["environment"]["AUTH_OIDC_GROUPS_ATTRIBUTE"] == "groups",
            "Homarr должен получать группы из OIDC")
    require(services["authelia"]["volumes"][-1] == (
        web["authelia"]["data"]["host"] + ":" + web["authelia"]["data"]["container"]
    ), "Данные Authelia с файлом пользователей должны быть доступны для записи")

    routes = environment.get_template("sites.generated.caddy.j2").render(**variables)
    require("home.zsergey.ru" in routes and "auth.zsergey.ru" in routes,
            "Нет основных доменов")
    require("photos.zsergey.ru" not in routes and "ai.zsergey.ru" not in routes,
            "Непроверенные службы не должны публиковаться по умолчанию")
    # Даже при открытом маршруте home мастер создания первого
    # администратора закрыт, пока не завершён штатный запуск Homarr.
    require("root /srv/homarr-catalog" in routes
            and "try_files /homarr-ready" in routes
            and "respond @onboarding" in routes
            and "/api/onboarding/*" in routes
            and "/api/trpc/group.createInitialExternalGroup*" in routes,
            "Homarr должен оставаться закрытым до завершения инициализации")
    require("reverse_proxy http://homarr:7575" in routes,
            "После готовности Homarr должен работать через Caddy")
    require(web["homarr"]["onboarding"]["admin_group"] == "admins",
            "Группа первичного администратора должна совпадать с Authelia")
    require(web["homarr"]["onboarding"]["ready_file"].startswith(
        web["paths"]["persistent_state"] + "/management/"
    ), "Отметка готовности должна находиться на постоянном томе")
    require(any(check.get("path") == web["homarr"]["onboarding"]["ready_file"]
                for check in checks["checks"]),
            "Готовность гостя 140 должна учитывать завершённую настройку Homarr")

    public_domains = web["caddy"]["public_domains"]
    require(set(public_domains) == {"home.zsergey.ru", "auth.zsergey.ru"},
            "По умолчанию публичны только приветствие и страница входа")
    require(all(
        route["access"] == (
            "public" if route["domain"] in public_domains
            else "app_auth" if route["domain"] == "photos.zsergey.ru"
            else "protected"
        )
        for route in web["caddy"]["routes"]
    ), "Начальные маршруты должны иметь явный режим доступа")

    # Сохранённый до обновления routes.yaml не содержит access.
    # Домены, не входящие в разрешённый список, обязаны получить защиту.
    legacy = [
        {key: value for key, value in route.items() if key != "access"}
        for route in web["caddy"]["routes"][:2]
    ]
    example_routes = legacy + [
        {"domain": "music.zsergey.ru", "upstream": "http://music:8080", "enabled": True},
        {"domain": "vpn.zsergey.ru", "upstream": "http://vpn:8080",
         "access": "private", "enabled": True},
    ]
    # Immich требует собственного входа: общий вход Caddy мешает мобильному приложению.
    immich_only = environment.get_template("sites.generated.caddy.j2").render(
        provision=provision, web_gateway_routes=[{
            "domain": "photos.zsergey.ru",
            "service": "immich",
            "upstream": "http://192.168.3.40:2283",
            "enabled": True, "access": "app_auth",
        }]
    )
    require("reverse_proxy http://192.168.3.40:2283" in immich_only
            and "forward_auth" not in immich_only,
            "Мобильный Immich нельзя отправлять через общий вход Caddy")
    caddy_example = environment.get_template("sites.generated.caddy.j2").render(
        provision=provision, web_gateway_routes=example_routes
    )
    home_segment = caddy_example.split("home.zsergey.ru {", 1)[1].split("\n}", 1)[0]
    auth_segment = caddy_example.split("auth.zsergey.ru {", 1)[1].split("\n}", 1)[0]
    music_segment = caddy_example.split("music.zsergey.ru {", 1)[1].split("\n}", 1)[0]
    vpn_segment = caddy_example.split("vpn.zsergey.ru {", 1)[1].split("\n}", 1)[0]
    require("forward_auth" not in home_segment,
            "Публичная страница Homarr должна оставаться открытой")
    require("@totp_setup path /totp-setup" in auth_segment
            and "@totp_lan remote_ip 192.168.0.0/16" in auth_segment
            and "reverse_proxy unix//run/totp-setup/setup.sock" in auth_segment
            and "forward_auth" not in auth_segment
            and "respond" in auth_segment and "403" in auth_segment,
            "QR должен открываться без пароля только из домашней сети")
    require("reverse_proxy http://authelia:9091" in auth_segment,
            "Вне начальной привязки Authelia должна принимать обычные запросы")
    require("forward_auth authelia:9091" in music_segment
            and "/api/authz/forward-auth" in music_segment,
            "Новый маршрут без access должен требовать входа через Authelia")
    require("reverse_proxy http://music:8080" in music_segment,
            "Разрешённые запросы должны доходить до защищённой службы")
    require("remote_ip 192.168.0.0/16" in vpn_segment
            and "respond" in vpn_segment and "403" in vpn_segment
            and "forward_auth" not in vpn_segment,
            "Закрытый маршрут обязан пропускать только домашнюю сеть")

    # Ansible включает trim_blocks=True, в отличие от обычного Jinja2.
    # Этот режим раньше склеивал raw-выражения секретов со следующими строками.
    authelia_environment = Environment(
        loader=FileSystemLoader(str(ROLE / "templates")),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
    )
    authelia = authelia_environment.get_template(
        "authelia-configuration.yml.j2"
    ).render(**variables)
    secret_expressions = re.findall(r"\{\{ secret [^\n]*? \}\}", authelia)
    require(len(secret_expressions) == 3,
            "Authelia должна сохранять ровно три выражения чтения секретов")
    # Эти выражения выполняет сама Authelia после Ansible. Для проверки
    # синтаксиса подменяем их безопасной строкой, не читая настоящие ключи.
    authelia_yaml = re.sub(
        r"\{\{ secret [^\n]*? \}\}",
        "'test-secret'",
        authelia,
    )
    parsed_authelia = yaml.safe_load(authelia_yaml)
    require(isinstance(parsed_authelia, dict),
            "Ansible должен выдавать корректный YAML Authelia")
    require(parsed_authelia["access_control"]["default_policy"] == "deny",
            "Не разрешать все домены Authelia по умолчанию")
    rules = parsed_authelia["access_control"]["rules"]
    require(len(rules) == 1
            and rules[0]["policy"] == "two_factor"
            and rules[0]["domain"] == "*.zsergey.ru",
            "Страница первичной привязки не требует исключений в Authelia")
    require(parsed_authelia["identity_providers"]["oidc"]["jwks"][0]["key"] == "test-secret",
            "JWKS не должен склеиваться со списком клиентов")
    oidc_clients = parsed_authelia["identity_providers"]["oidc"]["clients"]
    require({item["client_id"] for item in oidc_clients} == {"homarr", "immich"},
            "Не потерять клиентов Homarr и Immich при формировании YAML")
    require(all(item["client_secret"] == "test-secret" for item in oidc_clients),
            "Секреты OIDC не должны склеиваться с полем public")
    require("{{ secret " in authelia,
            "Authelia должна получать JWKS и хеш OIDC-клиента из файлов")
    for filename in (
        "authelia-oidc-jwks-pem",
        "authelia-homarr-client-secret-hash",
        "authelia-immich-client-secret-hash",
    ):
        require(secrets_container_path + "/" + filename in authelia,
                f"Шаблон Authelia должен читать {filename} с отдельного тома")
    require("/config/secrets/" not in authelia,
            "Шаблон Authelia не должен использовать старое вложенное подключение")
    require("client_secret:" in authelia and "authorization_policy: two_factor" in authelia,
            "Нужен защищённый OIDC-вход")
    require("/data/db.sqlite3" in authelia,
            "SQLite Authelia должна лежать на постоянном томе")
    require("path: /data/users_database.yml" in authelia,
            "Authelia должна работать с постоянным файлом пользователей")
    require("algorithm: argon2" in authelia and "variant: argon2id" in authelia,
            "Необходимо поддерживаемое имя алгоритма Argon2id")
    require("client_id: immich" in authelia and "app.immich:///oauth-callback" in authelia,
            "Нужен клиент Immich с мобильным адресом возврата")

    tasks = yaml.safe_load((ROLE / "tasks/main.yml").read_text(encoding="utf-8"))
    require(isinstance(tasks, list), "Роль Ansible должна содержать список задач")
    # Проверяем порядок: mountpoint должен предшествовать созданию каталогов.
    text = (ROLE / "tasks/main.yml").read_text(encoding="utf-8")
    require(text.find("mountpoint") < text.find("state: directory"),
            "Необходимо проверить реальное подключение до создания каталогов")
    require("src: local-home.html" in text and "caddy/local-home/index.html" in text,
            "Статическая страница должна устанавливаться ролью до запуска Caddy")
    require("src: totp-setup.html" in text and "initialize-totp.py" in text,
            "Штатная подготовка QR должна входить в общее развёртывание")
    setup_page = (ROLE / "files/totp-setup.html").read_text(encoding="utf-8")
    require(all(part in setup_page for part in (
        "Подключение телефона для входа",
        "Что нужно сделать",
        "Google Authenticator",
        "знак <strong>«+»</strong>",
        "квадратный рисунок",
        "Шесть цифр с телефона",
        "Подтвердить подключение",
        "15 минут",
        "Ничего удалять вручную не нужно",
        'action="/totp-setup/confirm"',
    )), "Страница должна объяснять подключение простыми словами")
    require("QR-код" not in setup_page and "TOTP" not in setup_page,
            "Не использовать специальные обозначения в тексте страницы")
    require(text.index("Запустить веб-шлюз") < text.index("Подготовить QR первого администратора"),
            "Генерация TOTP должна выполняться только после запуска контейнера Authelia")
    require("no_log: true" in text[text.index("Подготовить QR первого администратора"):],
            "Секрет QR нельзя печатать в журналы")
    require(web["authelia"]["totp_setup"]["username"] == "admin"
            and web["authelia"]["totp_setup"]["qr_host"].startswith(
                web["authelia"]["data"]["host"] + "/"
            ), "QR должен принадлежать первому администратору и лежать на mp0")
    require("totp-setup-socket:/run/totp-setup:ro" in str(services["caddy"]["volumes"]),
            "Caddy должен получать только временный Unix-сокет")
    require("/authelia/totp-setup:" not in str(services["caddy"]["volumes"]),
            "QR и URI нельзя напрямую монтировать в Caddy")
    unit = (ROLE / "templates/web-gateway-totp-setup.service.j2").read_text(encoding="utf-8")
    require("RuntimeMaxSec=900" in unit and "Restart=no" in unit,
            "Временный обработчик не должен работать более 15 минут")
    require("totp-setup-server.py" in text and "state: restarted" in text,
            "Обработчик должен запускаться только при незавершённой регистрации")
    require("totp-users.py" in text, "Администратору нужны операции для пользователей")
    require("local-home/widgets" in text
            and "authelia-users.json" in text
            and "caddy-routes.json" in text
            and "proxmox-guests.json" in text,
            "Все три несекретных виджета должны устанавливаться из Git на 140")
    probe = next((route for route in web["caddy"]["routes"]
                  if route["domain"] == "check.zsergey.ru"), None)
    require(isinstance(probe, dict) and probe.get("enabled") is False
            and probe.get("access") == "private"
            and probe.get("upstream") == "http://homarr:7575",
            "Пробный маршрут должен быть выключен и доступен только из домашней сети")
    for widget in ("authelia-users.json", "caddy-routes.json", "proxmox-guests.json"):
        definition = __import__("json").loads(
            (GUEST_DIR / "widgets" / widget).read_text(encoding="utf-8")
        )
        require(definition["$schema"] == "homarr-custom-widget-v2"
                and "template" in definition
                and definition["sources"]["default"]["networkScope"] == "private",
                "Несекретный виджет должен соответствовать формату Homarr v2")
    require("web_gateway_new_routes" in text
            and "Дополнить список только отсутствующими доменами" in text,
            "Добавление новых пробных маршрутов не должно сбрасывать постоянные настройки")
    require("manage-routes.py" in text
            and "sites.generated.caddy.j2" in text
            and "routes-policy.json" in text,
            "Для доменов должны устанавливаться проверка Caddy и перечень разрешённых адресов")
    require("python3-jinja2" in provision["system"]["required_packages"],
            "Для проверки доменов нужен штатный Jinja2")
    route_widget = __import__("json").loads(
        (GUEST_DIR / "widgets/caddy-routes.json").read_text(encoding="utf-8")
    )
    require(route_widget["requests"]["update"]["permission"] == "full"
            and route_widget["requests"]["update"]["kind"] == "action",
            "Изменение доменов разрешено только полноправному администратору")
    require("Запретить повторное развёртывание с утраченной базой Authelia" in text
            and "web_gateway_initial_state.stat.exists" in text,
            "После исчезновения SQLite повторный deploy должен остановиться")
    require("no_log: true" in text and "force: false" in text,
            "Нельзя раскрывать секреты или перезаписывать сохранённые маршруты")
    require(text.index("Закрыть публичный вход Homarr") < text.index("Запустить веб-шлюз")
            < text.index("Завершить первоначальную настройку Homarr"),
            "Публичный Homarr надо закрыть до его запуска и открыть после проверки")
    require("initialize-homarr.py" in text,
            "Первичная настройка должна выполняться из общего deploy 140")

    require("item.access | default(" in text
            and "provision.web_gateway.caddy.public_domains" in text
            and "'private'" in text,
            "Ansible обязан запрещать неожиданные и неразрешённые публичные режимы")
    require("web_gateway_secret_material" not in text and "web_gateway_users_database" not in text,
            "140 не должен ожидать материалы пользователей из OpenBao")
    require(
        all(name in text for name in ("initialize.py", "show-initial-password.py", "reset-admin-password.py")),
        "Нужны подготовка, просмотр и явная смена пароля без вывода в журнал",
    )
    for name in ["initialize.py", "show-initial-password.py", "reset-admin-password.py"]:
        filename = ROLE / "files" / name
        ast.parse(filename.read_text(encoding="utf-8"), filename=str(filename))
    init = (ROLE / "files" / "initialize.py").read_text(encoding="utf-8")
    require("O_EXCL" not in init and "os.link(name, path)" in init,
            "Защищённые ключи не должны заменяться обычной записью")
    require("authelia.validate" not in text,
            "Неожиданная временная команда")


    require(any(check.get("type") == "tcp" and check.get("port") == 8080
                for check in checks["checks"]),
            "Статус гостя должен проверять локальный HTTP-порт")
    status_names = {item["container"] for item in checks["checks"] if item["type"] == "docker"}
    compose_names = {item["container_name"] for item in services.values()}
    require(status_names == compose_names,
            "status.yaml должен проверять контейнеры из Compose")

    playbook = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
    require(any(
        task.get("ansible.builtin.include_role", {}).get("name") == "web_gateway"
        for task in playbook[0]["tasks"]
    ), "Общая настройка Linux должна подключать роль web_gateway")
    print("140: машинные YAML и шаблоны согласованы")


if __name__ == "__main__":
    test()
