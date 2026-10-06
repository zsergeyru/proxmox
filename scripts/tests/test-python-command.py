#!/usr/bin/env python3
"""Поведенческая проверка общей оболочки административных Python-команд."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "scripts/infra-manager/commands/python-command.sh"

CASES = {
    "infra-manager-status": ["status", "--full"],
}


def fail(message: str) -> None:
    raise SystemExit(message)


def main() -> None:
    if not WRAPPER.is_file():
        fail(f"Не найдена общая оболочка: {WRAPPER}")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        commands = root / "commands"
        package = root / "infra_manager"
        binary = root / "bin"
        commands.mkdir()
        package.mkdir()
        binary.mkdir()

        output = root / "python-argv.txt"
        fake_python = binary / "python3"
        fake_python.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$@\" > \"$FAKE_PYTHON_OUTPUT\"\n",
            encoding="utf-8",
        )
        fake_python.chmod(0o755)

        env = os.environ.copy()
        env["PATH"] = f"{binary}:{env.get('PATH', '')}"
        env["FAKE_PYTHON_OUTPUT"] = str(output)

        for installed_name, command_args in CASES.items():
            installed = commands / installed_name
            shutil.copy2(WRAPPER, installed)
            installed.chmod(0o755)
            result = subprocess.run(
                [str(installed), *command_args[1:]],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                fail(
                    f"{installed_name} завершилась с кодом {result.returncode}: "
                    f"{result.stderr.strip()}"
                )
            actual = output.read_text(encoding="utf-8").splitlines()
            expected = ["-m", "infra_manager", *command_args]
            if actual != expected:
                fail(
                    f"{installed_name} передала неверные аргументы Python: "
                    f"{actual!r}; ожидалось {expected!r}"
                )

        unknown = commands / "infra-manager-unknown"
        shutil.copy2(WRAPPER, unknown)
        unknown.chmod(0o755)
        result = subprocess.run(
            [str(unknown)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 2:
            fail("Общая оболочка должна отклонять неизвестное имя команды")

    print("[ОК] Общая оболочка административных команд проверена")


if __name__ == "__main__":
    main()
