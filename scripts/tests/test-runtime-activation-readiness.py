#!/usr/bin/env python3
"""Проверить ожидание подтверждённой активации 910 без реального PVE и Docker."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/infra-manager"))

from infra_manager import operator
from infra_manager.common import InfraManagerError

REF = "runtime:1234"


def test_wait(outcome: str | None, *, expected_ok: bool) -> None:
    """Нельзя принять отсутствие результата или неуспешную синхронизацию."""

    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        marker = directory / ".infra-manager-runtime-activation-pending"
        result = directory / ".infra-manager-runtime-activation-result"
        marker.write_text(REF, encoding="utf-8")

        def finish(_seconds: float) -> None:
            if outcome is not None:
                result.write_text(outcome, encoding="utf-8")
            marker.unlink()

        with (
            patch.object(operator, "RUNTIME_ACTIVATION_MARKER", marker),
            patch.object(operator, "RUNTIME_ACTIVATION_RESULT", result),
            patch.object(operator.time, "sleep", side_effect=finish),
        ):
            try:
                operator._await_runtime_activation(timeout_seconds=10)
            except InfraManagerError:
                if expected_ok:
                    raise
            else:
                if not expected_ok:
                    raise AssertionError("Неподтверждённая активация принята за успешную")


def test_refuse_no_marker() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        with patch.object(
            operator, "RUNTIME_ACTIVATION_MARKER", root / "missing-marker",
        ):
            try:
                operator._await_runtime_activation(timeout_seconds=1)
            except InfraManagerError:
                pass
            else:
                raise AssertionError("Нет маркера — нельзя объявлять готовность")


def test_timeout() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        marker = Path(temporary) / "marker"
        marker.write_text(REF, encoding="utf-8")
        with (
            patch.object(operator, "RUNTIME_ACTIVATION_MARKER", marker),
            patch.object(operator.time, "monotonic", side_effect=[0.0, 11.0]),
        ):
            try:
                operator._await_runtime_activation(timeout_seconds=10)
            except InfraManagerError as exc:
                assert "не завершилась" in str(exc)
            else:
                raise AssertionError("Бесконечное ожидание не допускается")


def test_activation_script_order() -> None:
    text = (
        ROOT / "scripts/infra-manager/commands/activate-runtime.sh"
    ).read_text(encoding="utf-8")
    # Все изменения Semaphore завершаются до записи результата.
    assert text.index("semaphore-project") < text.index("infra-manager-status --full --quiet")
    assert text.index('mv -f "$temp_result" "$ACTIVATION_RESULT"') < text.index(
        'rm -f "$ACTIVATION_MARKER"'
    )
    assert "trap finish_activation EXIT" in text
    assert "INFRA_PVE_NODE" in text


def main() -> None:
    test_wait(f"ok {REF}", expected_ok=True)
    test_wait(f"failed {REF}", expected_ok=False)
    test_wait("ok runtime:2222", expected_ok=False)
    test_wait(None, expected_ok=False)
    test_refuse_no_marker()
    test_timeout()
    test_activation_script_order()
    print("[ОК] Активация 910 и Semaphore должны завершиться до успешного итога")


if __name__ == "__main__":
    main()
