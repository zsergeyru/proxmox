#!/usr/bin/env python3
"""Завершить первый запуск Homarr через штатный интерфейс v2.5.2.

Вызывается только после запуска контейнера гостя 140 и только на localhost.
Создаёт группу admins и пустую закрытую доску, не меняет существующую базу
напрямую. В случае ошибки не создаёт признак готовности Caddy: публичный
маршрут останется недоступным. Повторный вызов безопасен.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener


class OnboardingError(Exception):
    """Ожидаемая ошибка первичной настройки без раскрытия данных сеанса."""


class HomarrClient:
    """Запрашивать локальный интерфейс Homarr, сохраняя закрытый cookie сеанса."""

    def __init__(self, base_url: str) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username:
            raise OnboardingError("Для первоначальной настройки разрешён только http://127.0.0.1")
        self.base_url = base_url.rstrip("/")
        self.opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, path: str, value: object | None = None) -> object:
        """Отправить запрос без переходов к чужим узлам и разбора закрытых ошибок."""
        is_post = value is not None
        payload = json.dumps(value, separators=(",", ":")).encode() if is_post else None
        headers = {"Content-Type": "application/json"} if is_post else {}
        request = Request(
            self.base_url + path,
            data=payload,
            headers=headers,
            method="POST" if is_post else "GET",
        )
        try:
            with self.opener.open(request, timeout=15) as response:
                if response.url.split("/")[2] != urlsplit(self.base_url).netloc:
                    raise OnboardingError("Homarr перенаправил запрос за пределы localhost")
                return json.load(response)
        except HTTPError as exc:
            raise OnboardingError(
                f"Homarr отказал в локальном запросе ({exc.code}); "
                "проверьте состояние его первичной настройки"
            ) from None
        except (URLError, OSError, ValueError, json.JSONDecodeError) as exc:
            raise OnboardingError("Homarr не отвечает локально или вернул неверный ответ") from exc

    def step(self) -> str:
        """Прочитать состояние первичной настройки через открытый запрос tRPC."""
        input_value = quote(json.dumps({"json": None}, separators=(",", ":")))
        result = self.request(f"/api/trpc/onboard.currentStep?input={input_value}")
        try:
            step = result["result"]["data"]["json"]["current"]
        except (KeyError, TypeError):
            raise OnboardingError("Неизвестный формат ответа Homarr v2.5.2") from None
        if step not in {"start", "group", "setup", "finish"}:
            raise OnboardingError(f"Неожиданный этап Homarr: {step!r}")
        return step

    def mutate(self, method: str, data: object) -> None:
        """Вызвать штатную операцию настройки под выданным Homarr cookie."""
        result = self.request("/api/trpc/" + method, {"json": data})
        if not isinstance(result, dict) or "error" in result or "result" not in result:
            raise OnboardingError(f"Homarr отказал выполнить {method}")

    def claim(self) -> None:
        """Получить временное право настройки с локального адреса."""
        result = self.request("/api/onboarding/claim", {})
        if not isinstance(result, dict) or result.get("status") not in {"issued", "active"}:
            raise OnboardingError("Homarr не выдал право первоначальной настройки")


def initialize(base_url: str, state_file: Path, admin_group: str) -> str:
    """Создать начальные настройки один раз, оставив Caddy закрытым при ошибке.

    Признак готовности не является базой: состояние сначала подтверждается
    через Homarr. Существующие пользователи, группы и доски не меняются.
    """
    if not state_file.parent.is_dir() or state_file.parent.is_symlink():
        raise OnboardingError("Отсутствует постоянный каталог готовности Homarr")
    state_file.unlink(missing_ok=True)
    client = HomarrClient(base_url)
    state = client.step()
    if state != "finish":
        client.claim()
        if state == "start":
            client.mutate("onboard.nextStep", {})
            state = client.step()
        if state == "group":
            client.mutate("group.createInitialExternalGroup", {"name": admin_group})
            state = client.step()
        if state == "setup":
            client.mutate(
                "onboard.completeSetup",
                {
                    "server": {
                        "defaultLocale": "ru",
                        "defaultColorScheme": "light",
                        "analyticsEnabled": False,
                    },
                    "board": {
                        "name": "dashboard",
                        "primaryColor": "#fa5252",
                        "secondaryColor": "#fd7e14",
                        "itemRadius": "lg",
                        "layoutPreset": "balanced",
                        "columnCount": 10,
                        "leftSidebar": False,
                        "rightSidebar": False,
                    },
                    "integrations": [],
                    "apps": [],
                    "selectedIntegrationIds": [],
                    "selectedAppIds": [],
                    "selectedDockerSourceIds": [],
                    "selectedWidgetKinds": [],
                },
            )
    if client.step() != "finish":
        raise OnboardingError("Homarr не завершил первоначальную настройку")
    temporary = state_file.with_name(state_file.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as fh:
        fh.write("ready\n")
    os.chmod(temporary, 0o644)
    os.replace(temporary, state_file)
    return "Homarr: первоначальная настройка завершена; публичный /init закрыт"


def main() -> int:
    """Проверить аргументы и вывести только итог без токенов и секретов."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:7575")
    parser.add_argument("--state-file", required=True, type=Path)
    parser.add_argument("--admin-group", default="admins")
    args = parser.parse_args()
    try:
        print(initialize(args.url, args.state_file, args.admin_group))
        return 0
    except OnboardingError as exc:
        parser.exit(1, f"140: {exc}. Публичный вход Homarr остаётся закрытым.\n")


if __name__ == "__main__":
    raise SystemExit(main())
