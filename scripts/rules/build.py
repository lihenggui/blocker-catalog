#!/usr/bin/env python3
"""Build the legacy snapshot, v2 catalog, and release archives offline."""

import argparse
import gzip
import hashlib
import io
import json
import re
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path

from rulelib import (
    ROOT, RuleError, json_text, load_component_sources, prepared,
    render_v2_components, render_v2_rule,
)

DEFAULT_PUBLISHED_AT = "1970-01-01T00:00:00Z"
OUTPUT_GROUPS = ("legacy", "v2", "release")
RETIRED_GROUPS = ("rules", "components", "component-details", "icon")
COPY_DIRECTORIES = ("legacy/components", "legacy/icon", "v2/icon")


def relative_files(directory):
    return {path.relative_to(directory) for path in directory.rglob("*") if path.is_file()}


def different_files(first, second):
    first_files, second_files = relative_files(first), relative_files(second)
    changed = first_files.symmetric_difference(second_files)
    changed.update(path for path in first_files & second_files
                   if (first / path).read_bytes() != (second / path).read_bytes())
    return sorted(changed)


def read_files(directory, prefix=""):
    return {prefix + path.as_posix(): (directory / path).read_bytes()
            for path in relative_files(directory)}


def file_records(files):
    return [{"path": path, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
            for path, data in sorted(files.items())]


def frozen_components(root):
    files = read_files(root / "legacy/components", "components/")
    checksums = root / "legacy/components.sha256"
    expected = "".join(f"{row['sha256']}  {row['path']}\n" for row in file_records(files))
    if checksums.read_text(encoding="utf-8") != expected:
        raise RuleError("legacy/components.sha256 does not match the frozen component tree")
    return files


def check_metadata(sequence, published_at):
    if type(sequence) is not int or sequence < 0:
        raise RuleError("sequence must be a non-negative integer")
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                            r"(?:\.[0-9]+)?Z", published_at):
            raise ValueError
        datetime.fromisoformat(published_at)
    except ValueError as exc:
        raise RuleError("publishedAt must be an ISO instant ending in Z") from exc


def check_rule_icons(rules, icons):
    for rule in rules:
        if "icon" in rule and rule["icon"] not in icons:
            raise RuleError(f"rule {rule['id']} references missing icon: {rule['icon']!r}")


def prepare_outputs(root):
    root = Path(root)
    sources, trackers, legacy_rules, warnings = prepared(root)
    rules = [render_v2_rule(rule, trackers) for _, rule in sources]
    components = render_v2_components(load_component_sources(root), legacy_rules["en"])
    icons = read_files(root / "data/icons", "icon/")
    check_rule_icons(rules, icons)
    frozen = frozen_components(root)
    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    return legacy_rules, frozen, rules, components, icons


def gzip_bytes(data):
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, compresslevel=9, mtime=0) as archive:
        archive.write(data)
    return buffer.getvalue()


def zip_icons(icons):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, data in sorted(icons.items()):
            entry = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return buffer.getvalue()


def catalog_files(rules, components, icons, sequence, source_commit, published_at):
    files = {"rules.json": json_text({"rules": rules}).encode("utf-8"), **icons}
    files["components.jsonl"] = "".join(
        json.dumps(component, ensure_ascii=False, separators=(",", ":")) + "\n"
        for component in components
    ).encode("utf-8")
    files["catalog.json"] = json_text({
        "format": 2,
        "sequence": sequence,
        "sourceCommit": source_commit,
        "publishedAt": published_at,
        "files": file_records(files),
    }).encode("utf-8")
    return files


def update_outputs(directory, files, check):
    if directory.is_symlink():
        raise RuleError(f"output directory is a symlink: {directory}")
    existing = set()
    for group in OUTPUT_GROUPS:
        folder = directory / group
        for path in (folder, *folder.rglob("*")):
            if path.is_symlink():
                raise RuleError(f"generated path is a symlink: {path}")
            if path.is_file():
                existing.add(path.relative_to(directory).as_posix())
    obsolete = existing - files.keys()
    changed = {name for name, data in files.items()
               if name not in existing or (directory / name).read_bytes() != data}
    retired = {name for name in RETIRED_GROUPS
               if (directory / name).exists() or (directory / name).is_symlink()}
    if check:
        stale = obsolete | changed | retired
        if stale:
            raise RuleError("generated files are out of date: " + ", ".join(sorted(stale)))
        return

    for name in obsolete | retired:
        path = directory / name
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    for name in sorted(changed):
        path = directory / name
        if path.is_dir():
            shutil.rmtree(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(files[name])
    for name in COPY_DIRECTORIES:
        (directory / name).mkdir(parents=True, exist_ok=True)


def build(root=ROOT, check=False, *, output_dir=None, sequence=0, source_commit="",
          published_at=DEFAULT_PUBLISHED_AT):
    check_metadata(sequence, published_at)
    if type(source_commit) is not str:
        raise RuleError("sourceCommit must be a string")
    root = Path(root)
    output_dir = root / "out" if output_dir is None else Path(output_dir)
    legacy_rules, frozen, rules, components, icons = prepare_outputs(root)
    legacy = {f"rules/{lang}/general.json": json_text(data).encode("utf-8")
              for lang, data in legacy_rules.items()}
    v2 = catalog_files(rules, components, icons, sequence, source_commit, published_at)
    release = {
        "catalog.json": v2["catalog.json"],
        "rules.json.gz": gzip_bytes(v2["rules.json"]),
        "components.jsonl.gz": gzip_bytes(v2["components.jsonl"]),
        "icons.zip": zip_icons(icons),
    }
    outputs = {f"{group}/{path}": data for group, files in (
        ("legacy", legacy | frozen | icons), ("v2", v2), ("release", release)
    ) for path, data in files.items()}
    update_outputs(output_dir, outputs, check)
    print(f"{'Checked' if check else 'Built'} {len(rules)} rules, {len(components)} v2 components, "
          f"and {len(frozen)} frozen component files")
    return legacy_rules


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--check", action="store_true", help="fail if generated files would change; never write")
    parser.add_argument("--sequence", type=int, default=0, help="non-negative catalog sequence (default: 0)")
    parser.add_argument("--source-commit", default="", help="source commit to record (default: empty)")
    parser.add_argument("--published-at", default=DEFAULT_PUBLISHED_AT,
                        help=f"ISO instant ending in Z (default: {DEFAULT_PUBLISHED_AT})")
    args = parser.parse_args()
    try:
        build(args.root, args.check, sequence=args.sequence, source_commit=args.source_commit,
              published_at=args.published_at)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"ERROR: {exc}\n")


if __name__ == "__main__":
    main()
