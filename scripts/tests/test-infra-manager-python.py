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


def fail(message: str) -> None:
    raise SystemExit(message)


def main_test() -> None:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        rc = main([])
    if rc != 0:
        fail(f"infra-manager CLI без аргументов вернул {rc}")
    if "Python-основа infra-manager" not in stdout.getvalue():
        fail("infra-manager CLI не вывел ожидаемую справку")

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
            fail(f"CommandError содержит неверный returncode: {exc.returncode}")
    else:
        fail("common.run не сообщил об ошибке внешней команды")

    direct = subprocess.run(
        [sys.executable, "-m", "infra_manager", "--help"],
        cwd=MODULE_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if direct.returncode != 0:
        fail(f"python -m infra_manager --help завершился с rc={direct.returncode}")
    if "usage: infra-manager" not in direct.stdout:
        fail("python -m infra_manager --help не вывел usage")

    print("infra-manager Python foundation tests passed.")


if __name__ == "__main__":
    main_test()
