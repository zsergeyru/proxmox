#!/usr/bin/env python3
"""Минимальные проверки Python-основы infra-manager."""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = ROOT / "scripts" / "infra-manager"
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager.cli import main  # noqa: E402
from infra_manager.common import CommandError, run  # noqa: E402
from infra_manager.setup import BASE_PACKAGES, Setup  # noqa: E402


def fail(message: str) -> None:
    raise SystemExit(message)


def main_test() -> None:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        rc = main([])
    if rc != 0 or "infra-manager" not in stdout.getvalue():
        fail("infra-manager CLI без аргументов не вывел справку")

    result = run(
        [sys.executable, "-c", "print('OK')"],
        capture_output=True,
    )
    if result.stdout.strip() != "OK":
        fail("common.run не вернул stdout внешней команды")

    try:
        run(
            [sys.executable, "-c", "import sys; sys.exit(7)"],
            capture_output=True,
        )
    except CommandError as exc:
        if exc.returncode != 7:
            fail(
                f"CommandError содержит неверный returncode: "
                f"{exc.returncode}"
            )
    else:
        fail("common.run не сообщил об ошибке внешней команды")

    for args in (["--help"], ["setup", "--help"]):
        direct = subprocess.run(
            [sys.executable, "-m", "infra_manager", *args],
            cwd=MODULE_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if direct.returncode != 0:
            fail(
                f"python -m infra_manager {' '.join(args)} "
                f"вернул {direct.returncode}"
            )
        if "usage: infra-manager" not in direct.stdout:
            fail(
                f"python -m infra_manager {' '.join(args)} "
                "не вывел usage"
            )

    if "python3" not in BASE_PACKAGES:
        fail("python3 отсутствует в контракте setup")
    if "python3-yaml" not in BASE_PACKAGES:
        fail("python3-yaml отсутствует в контракте setup")

    compose = Setup.compose("ps")
    if compose[:2] != ["docker", "compose"] or compose[-1] != "ps":
        fail(f"Некорректная команда Docker Compose: {compose!r}")

    print("infra-manager Python foundation tests passed.")


if __name__ == "__main__":
    main_test()
