"""Verify release assets and summarize catalog changes."""

import gzip
import hashlib
import io
import json
import zipfile
from collections import Counter

MANIFEST = "v2-catalog.json"
RULES = "v2-rules.json.gz"
COMPONENTS = "v2-components.jsonl.gz"
ICONS = "v2-icons.zip"
ASSETS = {
    MANIFEST: "application/json",
    RULES: "application/gzip",
    COMPONENTS: "application/gzip",
    ICONS: "application/zip",
}
DATA_FILES = {RULES: "rules.json", COMPONENTS: "components.jsonl"}
LIST_LIMIT = 8
KEYWORD_LIMIT = 6


class PublishError(ValueError):
    pass


def read_manifest(data, sequence):
    manifest = json.loads(data)
    if (type(manifest["format"]) is not int or manifest["format"] != 2
            or type(manifest["sequence"]) is not int or manifest["sequence"] != sequence):
        raise PublishError(f"{MANIFEST} does not describe format 2, sequence {sequence}")
    return manifest


def verify_payload(manifest, path, payload):
    expected = next((record for record in manifest["files"] if record["path"] == path), None)
    if (expected is None or expected["size"] != len(payload)
            or expected["sha256"] != hashlib.sha256(payload).hexdigest()):
        raise PublishError(f"{path} does not match the manifest hash and size")
    return payload


def unpack_data(manifest, name, data):
    return verify_payload(manifest, DATA_FILES[name], gzip.decompress(data))


def verify_assets(assets, sequence):
    manifest = read_manifest(assets[MANIFEST], sequence)
    for name in DATA_FILES:
        unpack_data(manifest, name, assets[name])
    expected = sorted(row["path"] for row in manifest["files"] if row["path"] not in DATA_FILES.values())
    with zipfile.ZipFile(io.BytesIO(assets[ICONS])) as archive:
        names = archive.namelist()
        if sorted(names) != expected or any(not name.startswith("icon/") for name in names):
            raise PublishError(f"{ICONS} paths do not match the manifest")
        for name in names:
            verify_payload(manifest, name, archive.read(name))
    return manifest


def markdown(text):
    special = "\\`*_[]<>~&"
    return "".join(f"\\{c}" if c in special else c for c in " ".join(text.split()))


def summarize(title, items):
    lines = [f"- {title} ({len(items)})"]
    lines.extend(f"  - {item}" for item in items[:LIST_LIMIT])
    if len(items) > LIST_LIMIT:
        lines.append(f"  - {len(items) - LIST_LIMIT} more")
    return lines


def rule_label(rule):
    return f"{rule['id']} · {markdown(rule['i18n']['en']['name'])}"


def keyword_changes(before, after):
    changes = []
    for sign, entries in (("+", Counter(after) - Counter(before)), ("−", Counter(before) - Counter(after))):
        changed = list(entries.elements())
        changes.extend(f"{sign} {markdown(keyword)}" for keyword in changed[:KEYWORD_LIMIT])
        if len(changed) > KEYWORD_LIMIT:
            changes.append(f"{sign} {len(changed) - KEYWORD_LIMIT} more ({len(changed)} total)")
    return ", ".join(changes) or "keyword order changed"


def text_only(before, after, metadata=()):
    ignored = {"i18n", *metadata}
    return (before["i18n"] != after["i18n"]
            and {k: v for k, v in before.items() if k not in ignored}
            == {k: v for k, v in after.items() if k not in ignored})


def release_notes(old_data, new_data):
    old_rules, new_rules = ({row["id"]: row for row in json.loads(data[RULES])["rules"]}
                            for data in (old_data, new_data))
    old_components, new_components = (
        {row["name"]: row for row in map(json.loads, data[COMPONENTS].splitlines())}
        for data in (old_data, new_data)
    )

    changed_rules = []
    rule_text = 0
    for key in sorted(old_rules.keys() & new_rules.keys()):
        before, after = old_rules[key], new_rules[key]
        if before == after:
            continue
        label = rule_label(after)
        if before["keywords"] != after["keywords"]:
            label += f" ({keyword_changes(before['keywords'], after['keywords'])})"
        changed_rules.append(label)
        rule_text += text_only(before, after)

    recommendations = []
    component_text = 0
    for name in sorted(old_components.keys() & new_components.keys()):
        before, after = old_components[name], new_components[name]
        if before["recommendToBlock"] != after["recommendToBlock"]:
            recommendations.append(
                f"{markdown(name)} ({str(before['recommendToBlock']).lower()} → "
                f"{str(after['recommendToBlock']).lower()})"
            )
        component_text += text_only(before, after, metadata=("updatedAt", "contributor"))

    lines = ["## Catalog changes", ""]
    lines += summarize("Rules added", [rule_label(new_rules[key]) for key in sorted(new_rules.keys() - old_rules.keys())])
    lines += summarize("Rules removed", [rule_label(old_rules[key]) for key in sorted(old_rules.keys() - new_rules.keys())])
    lines += summarize("Rules changed", changed_rules)
    lines += [f"- Components added ({len(new_components.keys() - old_components.keys())})",
              f"- Components removed ({len(old_components.keys() - new_components.keys())})"]
    lines += summarize("Blocking recommendations changed", recommendations)
    lines.append(f"- Text-only changes ({rule_text} rules, {component_text} components)")
    return "\n".join(lines) + "\n"
