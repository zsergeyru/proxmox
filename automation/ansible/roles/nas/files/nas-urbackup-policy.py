#!/usr/bin/env python3
"""Client-scoped UrBackup archival policy reconciler for a physical NAS.

Uses UrBackup's authenticated localhost web API. Never edits its SQLite
database, global retention or other clients. A conflicting preexisting
client archival policy is rejected rather than overwritten.
"""
from __future__ import annotations

import binascii
import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import urlencode, parse_qs
from urllib.request import Request, urlopen

POLICY = Path("/etc/nas-apps/urbackup/retention.json")
PASSWORD = Path("/etc/nas-apps/urbackup/admin-password")


class PolicyError(Exception):
    pass


def desired_rules(daily: int, weekly: int) -> dict[str, str]:
    """Keep one eligible file backup per day/week for 7 days/4 weeks."""
    if (type(daily) is not int or not 7 <= daily <= 365
            or type(weekly) is not int or not 4 <= weekly <= 104):
        raise PolicyError("Недопустимые сроки хранения локальных резервных копий")
    rules: dict[str, str] = {}
    for idx, (interval, interval_unit, duration, duration_unit) in enumerate(
        ((86400, "d", daily * 86400, "d"),
         (604800, "w", weekly * 604800, "w"))
    ):
        suffix = f"c{idx}"
        rules.update({
            "every_" + suffix: str(interval),
            "every_unit_" + suffix: interval_unit,
            "for_" + suffix: str(duration),
            "for_unit_" + suffix: duration_unit,
            "backup_type_" + suffix: "file",
            "window_" + suffix: "*;*;*;*",
            "letters_" + suffix: "",
        })
    return rules


def normalized_archive(value: str) -> dict[str, str]:
    """Ignore UrBackup-generated rule IDs and next-run timestamps only."""
    if not isinstance(value, str):
        raise PolicyError("UrBackup вернул неподдерживаемое описание архива")
    parsed = parse_qs(value, keep_blank_values=True, strict_parsing=True)
    if any(len(values) != 1 for values in parsed.values()):
        raise PolicyError("В архивных правилах найдены дубли ключей")
    allowed_extra = ("uuid_", "next_archival_", "timeleft_")
    return {key: values[0] for key, values in parsed.items()
            if not key.startswith(allowed_extra)}


def read_config(path: Path = POLICY) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PolicyError("Нет декларативной политики UrBackup на NAS") from exc
    if (not isinstance(raw, dict)
            or set(raw) != {"client_name", "admin_user", "web_port",
                           "keep_daily", "keep_weekly", "max_parallel_jobs"}
            or raw["admin_user"] != "admin"
            or raw["client_name"] != "nas-local"
            or raw["web_port"] != 55414
            or raw["max_parallel_jobs"] != 1):
        raise PolicyError("Неизвестные параметры декларативного UrBackup")
    desired_rules(raw["keep_daily"], raw["keep_weekly"])
    return raw


class Api:
    def __init__(self, port: int, username: str, password: str):
        self.url = f"http://127.0.0.1:{port}/x"
        self.username = username
        self.password = password
        self.session = ""

    def request(self, action: str, params: dict | None = None) -> dict:
        params = dict(params or {})
        if self.session:
            params["ses"] = self.session
        body = urlencode(params).encode("utf-8")
        request = Request(
            self.url + "?" + urlencode({"a": action}), data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urlopen(request, timeout=12) as response:
                if response.status != 200:
                    raise PolicyError("UrBackup API вернул ошибку")
                result = json.loads(response.read(1048577).decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise PolicyError("UrBackup API недоступен или вернул ошибку") from exc
        if not isinstance(result, dict):
            raise PolicyError("UrBackup API вернул неожиданный формат")
        return result

    def login(self) -> None:
        unauthenticated = self.request("login")
        if unauthenticated.get("success"):
            # Admin authentication must not be bypassed.
            raise PolicyError("UrBackup допускает анонимный доступ к административному API")
        salt = self.request("salt", {"username": self.username})
        if not all(isinstance(salt.get(key), str)
                   for key in ("ses", "salt", "rnd")):
            raise PolicyError("UrBackup не вернул проверяемые параметры авторизации")
        self.session = salt["ses"]
        raw = hashlib.md5((salt["salt"] + self.password).encode("utf-8")).digest()
        h = binascii.hexlify(raw).decode("ascii")
        rounds = salt.get("pbkdf2_rounds")
        if rounds is not None and int(rounds) > 0:
            if not 1 <= int(rounds) <= 1000000:
                raise PolicyError("Недопустимое число PBKDF2-итераций UrBackup")
            h = hashlib.pbkdf2_hmac(
                "sha256", raw, salt["salt"].encode("utf-8"), int(rounds)
            ).hex()
        digest = hashlib.md5((salt["rnd"] + h).encode("utf-8")).hexdigest()
        result = self.request("login", {"username": self.username, "password": digest})
        if not result.get("success"):
            raise PolicyError("UrBackup отклонил пароль администратора")


def reconcile(api: Api, plan: dict, *, apply: bool) -> bool:
    """Update exactly one registered client; no global-settings requests."""
    status = api.request("status").get("status")
    if not isinstance(status, list):
        raise PolicyError("UrBackup не вернул список зарегистрированных клиентов")
    matches = [client for client in status
               if isinstance(client, dict) and client.get("name") == plan["client_name"]]
    if len(matches) != 1 or type(matches[0].get("id")) is not int or matches[0]["id"] < 1:
        raise PolicyError("Не найден один зарегистрированный локальный клиент UrBackup")
    client_id = matches[0]["id"]
    settings = api.request("settings", {
        "sa": "clientsettings", "t_clientid": str(client_id),
    }).get("settings")
    if not isinstance(settings, dict):
        raise PolicyError("UrBackup не вернул настройки локального клиента")
    archive = settings.get("archive")
    if not isinstance(archive, dict):
        raise PolicyError("UrBackup не поддерживает клиентское архивирование")
    current_value = archive.get("value")
    if not isinstance(current_value, str) or type(archive.get("use")) is not int:
        raise PolicyError("Неизвестный формат архивных настроек клиента")
    desired = desired_rules(plan["keep_daily"], plan["keep_weekly"])
    current = normalized_archive(current_value)
    if current == desired and archive["use"] & 2:
        return False
    if current:
        # Existing user-defined local archival rules can preserve archives:
        # never silently replace/shorten their retention.
        raise PolicyError("У локального клиента уже есть другие архивные правила; отказ без изменений")
    if not apply:
        raise PolicyError("UrBackup: локальная политика хранения ещё не применена")

    result = api.request("settings", {
        "sa": "clientsettings_save",
        "t_clientid": str(client_id),
        "archive": urlencode(desired),
        "archive.use": "2",
        "overwrite": "true",
    })
    if "saved_ok" not in result:
        raise PolicyError("UrBackup не подтвердил применение политики хранения")
    after = api.request("settings", {
        "sa": "clientsettings", "t_clientid": str(client_id),
    }).get("settings")
    if (not isinstance(after, dict)
            or not isinstance(after.get("archive"), dict)
            or not (after["archive"].get("use", 0) & 2)
            or normalized_archive(after["archive"].get("value", "")) != desired):
        raise PolicyError("UrBackup не подтвердил политику клиента после записи")
    return True


def main() -> int:
    try:
        if len(sys.argv) != 2 or sys.argv[1] not in ("apply", "verify"):
            raise PolicyError("Доступно только apply или verify")
        policy = read_config()
        password = PASSWORD.read_text(encoding="utf-8").strip()
        if len(password) < 32:
            raise PolicyError("Не подтверждён пароль администратора UrBackup")
        api = Api(policy["web_port"], policy["admin_user"], password)
        api.login()
        changed = reconcile(api, policy, apply=sys.argv[1] == "apply")
        print("URBACKUP_POLICY_CHANGED:" if changed else
              "URBACKUP_POLICY_OK: настройки локального клиента подтверждены")
        return 0
    except (PolicyError, OSError, ValueError, TypeError) as exc:
        # Never print tokens, HTTP payloads or admin credentials.
        print("UrBackup: применение локальной политики остановлено — " +
              str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
