#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import shutil
import tempfile
import urllib.request
from pathlib import Path

import yaml

COMMENT_PREFIXES = ("#", ";")


def _clean_lines(text: str) -> list[str]:
    result = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(COMMENT_PREFIXES):
            continue
        result.append(line)
    return result


def _domain(value: str) -> str:
    value = value.strip().lower().rstrip(".")
    if value.startswith("*."):
        value = value[2:]
    if not value or " " in value or "/" in value:
        raise ValueError(f"недопустимый домен: {value!r}")
    return value


def normalize(text: str, fmt: str) -> list[str]:
    values: set[str] = set()
    if fmt == "domain-list":
        for line in _clean_lines(text):
            values.add(_domain(line.split()[0]))
    elif fmt == "hosts":
        for line in _clean_lines(text):
            parts = line.split()
            if len(parts) < 2:
                raise ValueError(f"недопустимая hosts-строка: {line!r}")
            ipaddress.ip_address(parts[0])
            for name in parts[1:]:
                values.add(_domain(name))
    elif fmt == "ip-list":
        for line in _clean_lines(text):
            values.add(str(ipaddress.ip_address(line.split()[0])))
    elif fmt == "cidr-list":
        for line in _clean_lines(text):
            values.add(str(ipaddress.ip_network(line.split()[0], strict=False)))
    else:
        raise ValueError(f"неподдерживаемый формат: {fmt}")
    return sorted(values)


def read_source(source: dict) -> str:
    source_type = source["type"]
    if source_type == "file":
        return Path(source["path"]).read_text(encoding="utf-8")
    if source_type == "http":
        request = urllib.request.Request(
            source["url"],
            headers={"User-Agent": "network-gateway-list-updater/1"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8")
    raise ValueError(f"неподдерживаемый тип источника: {source_type}")


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _last_good_path(state_dir: Path, name: str) -> Path:
    return state_dir / "last-good" / f"{name}.json"


def load_source(source: dict, state_dir: Path, keep_last_good: bool) -> dict:
    last_good = _last_good_path(state_dir, source["name"])
    try:
        raw = read_source(source)
        values = normalize(raw, source["format"])
        if not values:
            raise ValueError("источник пуст после нормализации")
        result = {
            "name": source["name"],
            "format": source["format"],
            "target": source["target"],
            "priority": int(source.get("priority", 100)),
            "values": values,
            "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "from_last_good": False,
        }
        _write_atomic(last_good, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return result
    except Exception:
        if keep_last_good and last_good.exists():
            result = json.loads(last_good.read_text(encoding="utf-8"))
            result["from_last_good"] = True
            return result
        raise


def merge_sources(items: list[dict]) -> dict[str, dict[str, str]]:
    merged: dict[str, dict[str, str]] = {}
    for item in items:
        for value in item["values"]:
            key = f'{item["format"]}:{value}'
            candidate = {
                "value": value,
                "format": item["format"],
                "target": item["target"],
                "source": item["name"],
                "priority": int(item["priority"]),
            }
            current = merged.get(key)
            if current is None or candidate["priority"] > current["priority"]:
                merged[key] = candidate
            elif candidate["priority"] == current["priority"] and candidate["target"] != current["target"]:
                raise ValueError(
                    f"конфликт одинакового приоритета для {value}: "
                    f'{current["target"]} / {candidate["target"]}'
                )
    return merged


def render(config: dict, state_dir: Path, output_dir: Path) -> None:
    routing = config["routing"]
    keep_last_good = bool(routing["update"].get("keep_last_good", True))
    loaded = [
        load_source(source, state_dir, keep_last_good)
        for source in routing.get("sources", [])
    ]
    merged = merge_sources(loaded)

    target_values: dict[str, dict[str, list[str]]] = {
        name: {} for name in routing.get("targets", {})
    }
    for entry in merged.values():
        target = target_values.setdefault(entry["target"], {})
        target.setdefault(entry["format"], []).append(entry["value"])

    staging = Path(tempfile.mkdtemp(prefix=".routing.", dir=output_dir.parent))
    try:
        for target, formats in target_values.items():
            target_dir = staging / target
            target_dir.mkdir(parents=True, exist_ok=True)
            domains = sorted(
                set(formats.get("domain-list", [])) | set(formats.get("hosts", []))
            )
            (target_dir / "domains.txt").write_text(
                ("\n".join(domains) + "\n") if domains else "",
                encoding="utf-8",
            )
            for fmt, values in formats.items():
                path = staging / target / f"{fmt}.txt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("\n".join(sorted(set(values))) + "\n", encoding="utf-8")
        manifest = {
            "default": routing["default"],
            "sources": loaded,
            "targets": sorted(target_values),
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        backup = output_dir.with_name(output_dir.name + ".previous")
        if backup.exists():
            shutil.rmtree(backup)
        if output_dir.exists():
            os.replace(output_dir, backup)
        os.replace(staging, output_dir)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    state_dir = Path(args.state_dir)
    output_dir = Path(args.output_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    render(config, state_dir, output_dir)


if __name__ == "__main__":
    main()
