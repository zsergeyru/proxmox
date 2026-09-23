#!/usr/bin/env python3
"""Render the Debian template builder Cloud-Init from versioned assets."""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

import yaml

PLACEHOLDERS = (
    "__TEMPLATE_BOOTSTRAP_CONTENT__",
    "__TEMPLATE_FINALIZE_CONTENT__",
    "__TEMPLATE_VERSION__",
    "__IMAGE_NAME__",
    "__IMAGE_SHA512__",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--bootstrap", required=True, type=Path)
    parser.add_argument("--finalize", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--template-version", required=True)
    parser.add_argument("--image-name", required=True)
    parser.add_argument("--image-sha512", required=True)
    return parser.parse_args()


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"cannot read {path}: {exc}") from exc


def render(args: argparse.Namespace) -> str:
    if len(args.image_sha512) != 128 or any(
        char not in "0123456789abcdefABCDEF" for char in args.image_sha512
    ):
        raise SystemExit("image SHA-512 must contain exactly 128 hexadecimal characters")

    try:
        data = yaml.safe_load(read_text(args.base))
    except yaml.YAMLError as exc:
        raise SystemExit(f"invalid YAML in {args.base}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"{args.base} must contain a YAML mapping")

    bootstrap = read_text(args.bootstrap)
    bootstrap = (
        bootstrap.replace("__TEMPLATE_VERSION__", args.template_version)
        .replace("__IMAGE_NAME__", args.image_name)
        .replace("__IMAGE_SHA512__", args.image_sha512)
    )
    finalize = read_text(args.finalize)

    replacement = {
        "/usr/local/sbin/template-bootstrap": bootstrap,
        "/usr/local/sbin/template-finalize": finalize,
    }
    counts = {path: 0 for path in replacement}

    write_files = data.get("write_files")
    if not isinstance(write_files, list):
        raise SystemExit("cloud-init base must contain write_files as a list")

    for item in write_files:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if path in replacement:
            counts[path] += 1
            item["content"] = replacement[path]

    bad_counts = {path: count for path, count in counts.items() if count != 1}
    if bad_counts:
        raise SystemExit(
            "Cloud-Init guest script entries must occur exactly once: "
            + ", ".join(f"{path}={count}" for path, count in sorted(bad_counts.items()))
        )

    rendered = "#cloud-config\n" + yaml.safe_dump(
        data,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    for marker in PLACEHOLDERS:
        if marker in rendered:
            raise SystemExit(f"unresolved template marker: {marker}")

    try:
        parsed = yaml.safe_load(rendered)
    except yaml.YAMLError as exc:
        raise SystemExit(f"rendered Cloud-Init is invalid YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SystemExit("rendered Cloud-Init must be a YAML mapping")

    return rendered


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def main() -> int:
    args = parse_args()
    atomic_write(args.output, render(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
