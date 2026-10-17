#!/usr/bin/env python3
"""Не публиковать Immich до безопасного связывания существующих аккаунтов."""
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[2]


def test():
    gateway = yaml.safe_load((ROOT / "infrastructure/guests/140-web-gateway/provision.yaml").read_text())
    archive = yaml.safe_load((ROOT / "infrastructure/guests/340-photo-archive/provision.yaml").read_text())
    route = next(r for r in gateway["web_gateway"]["caddy"]["routes"]
                 if r["domain"] == "photos.zsergey.ru")
    auth = archive["photo_archive"]["oauth"]
    assert route["service"] == "immich" and route["guest_vmid"] == 340
    assert route["access"] == "app_auth", "Мобильный API нельзя закрывать общим входом Caddy"
    assert route["enabled"] is False, "Нельзя открывать Immich без готовой OAuth-настройки"
    assert auth["enabled"] is False, "Нельзя автоматически менять вход существующих пользователей"
    assert auth["auto_register"] is False, "Нельзя создавать дубликаты учётных записей"
    assert auth["keep_password_login"] is True, "Сохранить возможность прежнего входа"
    assert auth["provider_guest_vmid"] == 140
    assert auth["issuer_url"] == "https://auth.zsergey.ru"
    assert auth["mobile_redirect"] == "app.immich:///oauth-callback"
    accounts = archive["photo_archive"]["initial_setup"]
    assert len(accounts["users"]) == 2 and accounts["admin"]["email"]
    authelia = (ROOT / "automation/ansible/roles/web_gateway/templates/authelia-configuration.yml.j2").read_text()
    assert "client_id: immich" in authelia
    assert "https://photos.zsergey.ru/auth/login" in authelia
    assert "app.immich:///oauth-callback" in authelia
    print("140/340: Immich не открыт и не создаёт дубликатов; OIDC подготовлен")


if __name__ == "__main__":
    test()
