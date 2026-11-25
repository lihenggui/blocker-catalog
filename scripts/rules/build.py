#!/usr/bin/env python3
"""Generate the legacy app JSON and component bundles; --check never writes."""

import argparse
import shutil
import sys
from pathlib import Path

from rulelib import (
    ROOT, RuleError, json_text, load_component_sources, prepared, render_components,
    stale_component_outputs, stale_outputs, write_component_outputs, write_text,
)


def prepare_outputs(root):
    _, _, rendered, warnings = prepared(root)
    components = render_components(load_component_sources(root), rendered)
    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    return rendered, components


def relative_files(directory):
    return {path.relative_to(directory) for path in directory.rglob("*") if path.is_file()}


def different_files(first, second):
    first_files, second_files = relative_files(first), relative_files(second)
    changed = first_files.symmetric_difference(second_files)
    changed.update(path for path in first_files & second_files
                   if (first / path).read_bytes() != (second / path).read_bytes())
    return sorted(changed)


def build(root=ROOT, check=False, *, output_dir=None):
    root = Path(root)
    output_dir = root / "out" if output_dir is None else Path(output_dir)
    rendered, components = prepare_outputs(root)
    icons = root / "data/icons"
    output_icons = output_dir / "icon"
    if check:
        stale = stale_outputs(output_dir, rendered) + stale_component_outputs(output_dir, components)
        stale.extend((Path("icon") / path).as_posix() for path in different_files(icons, output_icons))
        if stale:
            raise RuleError("generated files are out of date: " + ", ".join(stale))
    else:
        for lang, data in rendered.items():
            write_text(output_dir / f"rules/{lang}/general.json", json_text(data))
        write_component_outputs(output_dir, components)
        if output_icons.exists():
            shutil.rmtree(output_icons)
        if icons.is_dir():
            shutil.copytree(icons, output_icons)
    print(f"{'Checked' if check else 'Built'} {len(rendered['en'])} rules and "
          f"{len(components['en'])} components in {len(rendered)} languages")
    return rendered


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--check", action="store_true", help="fail if generated files would change; never write")
    args = parser.parse_args()
    try:
        build(args.root, args.check)
    except (RuleError, OSError, ValueError) as exc:
        parser.exit(1, f"ERROR: {exc}\n")


if __name__ == "__main__":
    main()
