#!/usr/bin/env python3
"""Validate catalog sources and optionally verify two clean builds."""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from build import (
    build, check_metadata, check_rule_icons, different_files, file_records, prepare_outputs, read_files,
)
from rulelib import COMPONENT_TEXT_FIELDS, ROOT, TEXT_FIELDS, RuleError

RULE_FIELDS = {
    "id": int, "icon": str, "website": str, "keywords": list,
    "safeToBlock": bool, "contributors": list, "i18n": dict,
}
COMPONENT_FIELDS = {
    "name": str, "sdkRuleId": int, "recommendToBlock": bool,
    "contributor": str, "updatedAt": str, "i18n": dict,
}
MANIFEST_FIELDS = {"format": int, "sequence": int, "sourceCommit": str, "publishedAt": str, "files": list}


def check_fields(obj, fields, required, label):
    if type(obj) is not dict:
        raise RuleError(f"{label}: expected an object")
    missing = set(required) - obj.keys()
    if missing:
        raise RuleError(f"{label}: missing fields: {', '.join(sorted(missing))}")
    for field, value in obj.items():
        if field not in fields:
            raise RuleError(f"{label}: unexpected field {field}")
        if type(value) is not fields[field]:
            raise RuleError(f"{label}.{field}: expected {fields[field].__name__}")


def check_i18n(i18n, text_fields, required_en, label):
    check_fields(i18n, {"en": dict, "zh-CN": dict}, ("en",), label)
    fields = dict.fromkeys(text_fields, str)
    check_fields(i18n["en"], fields, (required_en,), f"{label}.en")
    if "zh-CN" in i18n:
        check_fields(i18n["zh-CN"], fields, (), f"{label}.zh-CN")
        if not i18n["zh-CN"]:
            raise RuleError(f"{label}.zh-CN: omit empty language objects")


def check_order(values, label):
    if values != sorted(set(values)):
        raise RuleError(f"{label} must be sorted and unique")


def check_v2(directory):
    files = read_files(directory)
    manifest = json.loads(files.pop("catalog.json"))
    check_fields(manifest, MANIFEST_FIELDS, MANIFEST_FIELDS, "catalog.json")
    if manifest["format"] != 2:
        raise RuleError("catalog.json: format must be 2")
    check_metadata(manifest["sequence"], manifest["publishedAt"])
    entry_fields = {"path": str, "sha256": str, "size": int}
    for entry in manifest["files"]:
        check_fields(entry, entry_fields, entry_fields, "catalog.json file entry")
    if manifest["files"] != file_records(files):
        raise RuleError("catalog.json: file paths, hashes or sizes differ from the v2 payload")

    payload = json.loads(files["rules.json"])
    check_fields(payload, {"rules": list}, ("rules",), "rules.json")
    ids = []
    for row in payload["rules"]:
        check_fields(row, RULE_FIELDS, ("id", "keywords", "contributors", "i18n"), "rule")
        label = f"rule {row['id']}"
        check_i18n(row["i18n"], TEXT_FIELDS, "name", f"{label}.i18n")
        if row["id"] <= 0 or not row["i18n"]["en"]["name"].strip():
            raise RuleError(f"{label}: expected a positive id and a nonempty English name")
        for field in ("keywords", "contributors"):
            if any(type(value) is not str for value in row[field]):
                raise RuleError(f"{label}.{field}: expected an array of strings")
        ids.append(row["id"])
    check_order(ids, "rule ids")
    check_rule_icons(payload["rules"], {path for path in files if path.startswith("icon/")})

    valid_ids = set(ids)
    names = []
    for index, line in enumerate(files["components.jsonl"].splitlines(), start=1):
        row = json.loads(line)
        label = f"components.jsonl line {index}"
        check_fields(row, COMPONENT_FIELDS, ("name", "recommendToBlock", "i18n"), label)
        if list(row) != [field for field in COMPONENT_FIELDS if field in row]:
            raise RuleError(f"{label}: unexpected field order")
        check_i18n(row["i18n"], COMPONENT_TEXT_FIELDS, "description", f"{label}.i18n")
        if "sdkRuleId" in row and row["sdkRuleId"] not in valid_ids:
            raise RuleError(f"{label}: unknown sdkRuleId {row['sdkRuleId']}")
        names.append(row["name"])
    check_order(names, "component names")


def check_determinism(root):
    with TemporaryDirectory(prefix="catalog-validation-") as directory:
        first, second = Path(directory) / "first", Path(directory) / "second"
        build(root, output_dir=first)
        build(root, output_dir=second)
        changed = different_files(first, second)
        if changed:
            raise RuleError("builds differ: " + ", ".join(path.as_posix() for path in changed))
        check_v2(first / "v2")
    print("Two builds are byte-identical. V2 catalog and frozen components validated.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--check", action="store_true", help="compare two clean builds and verify the v2 catalog")
    args = parser.parse_args()
    try:
        if args.check:
            check_determinism(args.root)
        else:
            _, frozen, rules, components, _ = prepare_outputs(args.root)
            print(f"Validated {len(rules)} rules, {len(components)} v2 components, "
                  f"and {len(frozen)} frozen component files")
    except (KeyError, OSError, ValueError) as exc:
        parser.exit(1, f"ERROR: {exc}\n")


if __name__ == "__main__":
    main()
