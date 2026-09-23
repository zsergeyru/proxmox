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

from guest_config import GuestConfigError, resolve_effective_guest  # noqa: E402


def load_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: ожидается YAML mapping")
    return data


def build_payload(root: Path) -> dict:
    guests_dir = root / "guests"
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
            raise ValueError(f"{manifest}: итоговый vmid должен быть integer")

        key = str(vmid)
        if key in result:
            raise ValueError(f"дублирующий vmid в итоговом состоянии: {vmid}")

        result[key] = effective

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
    args = parser.parse_args()

    try:
        payload = build_payload(REPO_ROOT)
    except (OSError, ValueError, GuestConfigError, yaml.YAMLError) as exc:
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
