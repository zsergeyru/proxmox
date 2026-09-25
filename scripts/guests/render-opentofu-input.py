#!/usr/bin/env python3
"""Преобразует guest.yaml в простой входной JSON для OpenTofu."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "guests"))

from resolver import GuestConfigError, resolve_effective_guest


def load_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"{path}: ожидается YAML mapping")
    return data


def build_payload(
    root: Path,
    *,
    include_vmids: set[int] | None = None,
    exclude_vmids: set[int] | None = None,
) -> dict:
    """Собрать итоговое состояние с явным фильтром владельца state."""
    include = set(include_vmids) if include_vmids is not None else None
    exclude = set(exclude_vmids or ())
    if include is not None and include & exclude:
        overlap = ", ".join(str(value) for value in sorted(include & exclude))
        raise ValueError(f"VMID одновременно включены и исключены: {overlap}")

    guests_dir = root / "infrastructure" / "guests"
    defaults = load_yaml(guests_dir / "defaults.yaml")
    result: dict[str, dict] = {}

    for directory in sorted(guests_dir.iterdir()):
        if not directory.is_dir():
            continue

        manifest = directory / "guest.yaml"
        if not manifest.is_file():
            continue

        source = load_yaml(manifest)
        if "profile" not in source:
            continue

        resolved = resolve_effective_guest(source, defaults)
        effective = resolved.effective
        vmid = effective.get("vmid")

        if not isinstance(vmid, int):
            raise TypeError(f"{manifest}: итоговый vmid должен быть integer")
        if include is not None and vmid not in include:
            continue
        if vmid in exclude:
            continue

        key = str(vmid)
        if key in result:
            raise ValueError(f"дублирующий vmid в итоговом состоянии: {vmid}")

        result[key] = effective

    if include is not None:
        missing = sorted(include - {int(value) for value in result})
        if missing:
            values = ", ".join(str(value) for value in missing)
            raise ValueError(
                f"Запрошенные VMID отсутствуют в управляемом состоянии: {values}"
            )

    return {
        "format_version": 1,
        "guests": result,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Собрать итоговое состояние управляемых гостей для OpenTofu"
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Файл результата. Без параметра JSON выводится в stdout.",
    )
    parser.add_argument(
        "--include-vmid",
        type=int,
        action="append",
        default=[],
        help="Включить только указанный VMID. Параметр можно повторять.",
    )
    parser.add_argument(
        "--exclude-vmid",
        type=int,
        action="append",
        default=[],
        help="Исключить VMID из результата. Параметр можно повторять.",
    )
    args = parser.parse_args()

    try:
        payload = build_payload(
            REPO_ROOT,
            include_vmids=set(args.include_vmid) if args.include_vmid else None,
            exclude_vmids=set(args.exclude_vmid),
        )
    except (
        OSError,
        TypeError,
        ValueError,
        GuestConfigError,
        yaml.YAMLError,
    ) as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1

    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
