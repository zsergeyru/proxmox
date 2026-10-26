#!/usr/bin/env python3
"""Offline preservation-only PVE admission; never request a destructive answer."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/infra-manager/host"))
from infra_manager_nas_storage import confirm_nas_storage

ROLES = ("system", "shared", "data", "backup")
SERIALS = ("SSD-TEST", "Z4Y4DB7V", "WW692JZ4", "WW69BCHK")
FINGERPRINT = "e" * 64


class UnsafePlan(ValueError):
    pass


def sample(**changes) -> dict:
    return {
        "host": "nas",
        "fingerprint": FINGERPRINT,
        "requires_creation": False,
        "items": [
            {"role": role, "serial": serial, "action": "preserve"}
            for role, serial in zip(ROLES, SERIALS)
        ],
        **changes,
    }


def run(plan: dict):
    calls = []

    def forward(command, **options):
        calls.append((command, options))
        if options.get("capture_output"):
            return SimpleNamespace(
                returncode=0,
                stdout="PHYSICAL_STORAGE_PLAN_JSON=" + json.dumps(plan) + "\n",
                stderr="",
            )
        return 0

    result = confirm_nas_storage("nas", forward, UnsafePlan)
    return result, calls


def reject(plan: dict):
    try:
        run(plan)
    except UnsafePlan:
        return
    raise AssertionError("Dangerous or ambiguous NAS plan was accepted")


def main() -> int:
    result, calls = run(sample())
    assert result == 0 and len(calls) == 2
    assert calls[0][0] == ["deploy", "nas", "--_physical_stage", "plan"]
    assert calls[1][0] == ["deploy", "nas", "--_physical_stage", "prepare"]
    assert json.loads(calls[1][1]["input_text"]) == {"fingerprint": FINGERPRINT}
    assert run(sample(recovery_fingerprint=FINGERPRINT))[0] == 0
    reject(sample(requires_creation=True))
    reject(sample(items=[
        {"role": r, "serial": s, "action": ("recreate" if r == "data" else "preserve")}
        for r, s in zip(ROLES, SERIALS)
    ]))
    reject(sample(items=[
        {"role": r, "serial": s, "action": ("create-zfs" if r == "data" else "preserve")}
        for r, s in zip(ROLES, SERIALS)
    ]))
    reject(sample(items=[
        {"role": r, "serial": SERIALS[0], "action": "preserve"}
        for r in ROLES
    ]))
    reject(sample(recovery_fingerprint="f" * 64))
    reject(sample(host="other"))
    reject(sample(fingerprint="invalid"))
    reject(sample(items=[]))
    source = (ROOT / "scripts/infra-manager/host/infra_manager_nas_storage.py").read_text()
    assert "input(" not in source
    assert "recreate" not in source or "recreate" in source.split("raise error_type")[0]
    assert "подтвердить очистку" not in source.casefold()
    print("[ОК] PVE принимает только существующие HDD без форматирования")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
