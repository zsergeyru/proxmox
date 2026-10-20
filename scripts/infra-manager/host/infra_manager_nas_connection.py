"""Интерактивная первичная настройка SSH-подключения NAS на PVE."""

from __future__ import annotations

import ipaddress
import json
import re
import sys

CONNECTION_REQUIRED = 21
_FINGERPRINT = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")


def initial_nas_connection(name: str, forward, error_type: type[Exception]) -> int:
    """Не менять Git; один раз запросить адрес и независимо проверенный ключ."""

    state = forward(["connection-state", name])
    if state == 0:
        return 0
    if state != CONNECTION_REQUIRED:
        return state
    if not sys.stdin.isatty():
        raise error_type(
            "Для первого подключения NAS необходим терминал PVE. "
            "При удалённом запуске используйте ssh -tt root@pve "
            "'infra-manager deploy nas'"
        )
    print("\n[ИНФО] NAS ещё не подключён к Infra Manager.")
    address = input("IP-адрес NAS: ").strip()
    try:
        ipaddress.ip_address(address)
    except ValueError as exc:
        raise error_type("Укажите действительный IPv4- или IPv6-адрес NAS") from exc
    print(
        "Проверьте SSH-отпечаток на консоли самого NAS командой:\n"
        "  ssh-keygen -E sha256 -lf /etc/ssh/ssh_host_ed25519_key.pub\n"
        "Введите отпечаток SHA256:... с NAS, а не скопированный из сети."
    )
    fingerprint = input("Проверенный SSH-отпечаток NAS: ").strip()
    if not _FINGERPRINT.fullmatch(fingerprint):
        raise error_type("Неверный SSH-отпечаток (требуется SHA256:...)")
    payload = json.dumps(
        {"address": address, "fingerprint": fingerprint}
    ) + "\n"
    return forward(["connection-setup", name], input_text=payload)
