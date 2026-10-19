#!/usr/bin/env python3
"""Одноразовое получение снимка OpenBao из сети loopback управляющего LXC.

Программа выполняется через pct exec, получает PVE-only AppRole через stdin,
передаёт только бинарный снимок Raft через stdout. Файлов секретов не создаёт.
"""

from __future__ import annotations

import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "http://127.0.0.1:8200"
MAX_BYTES = 2 * 1024 * 1024 * 1024


def send(method: str, path: str, *, token: str | None = None, data=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Vault-Token"] = token
    encoded = json.dumps(data).encode() if data is not None else None
    req = Request(BASE + path, headers=headers, data=encoded, method=method)
    return urlopen(req, timeout=180)


def main() -> int:
    try:
        creds = json.load(sys.stdin)
        if not isinstance(creds, dict) or set(creds) != {"role_id", "secret_id"}:
            raise ValueError("invalid credentials")
        for value in creds.values():
            if not isinstance(value, str) or not value:
                raise ValueError("empty credentials")
        with send(
            "POST", "/v1/auth/infra-manager/login",
            data=creds,
        ) as response:
            token = json.load(response)["auth"]["client_token"]
        if not isinstance(token, str) or not token:
            raise ValueError("invalid session")
        try:
            size = 0
            with send("GET", "/v1/sys/storage/raft/snapshot", token=token) as response:
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    size += len(block)
                    if size > MAX_BYTES:
                        raise ValueError("snapshot too large")
                    sys.stdout.buffer.write(block)
            sys.stdout.buffer.flush()
            if size < 256:
                raise ValueError("empty snapshot")
        finally:
            try:
                with send(
                    "POST", "/v1/auth/token/revoke-self", token=token, data={},
                ):
                    pass
            except (OSError, ValueError, HTTPError, URLError):
                pass
        return 0
    except (OSError, ValueError, KeyError, TypeError, HTTPError, URLError) as exc:
        # Исключения могут включать URL или служебный ответ. Не печатать
        # их при резервировании, чтобы избежать раскрытия токена.
        print("ОШИБКА: не удалось снять Raft через ограниченный AppRole", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
