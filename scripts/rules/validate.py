#!/usr/bin/env python3
"""Validate rule and component sources and optionally check build determinism."""

import argparse
from pathlib import Path
from tempfile import TemporaryDirectory

from build import build, different_files, prepare_outputs
from rulelib import ROOT, RuleError


def check_determinism(root):
    with TemporaryDirectory(prefix="catalog-validation-") as directory:
        first, second = Path(directory) / "first", Path(directory) / "second"
        build(root, output_dir=first)
        build(root, output_dir=second)
        changed = different_files(first, second)
        if changed:
            raise RuleError("builds differ: " + ", ".join(path.as_posix() for path in changed))
    print("Two builds are byte-identical")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--check", action="store_true", help="compare two clean builds byte for byte")
    args = parser.parse_args()
    try:
        if args.check:
            check_determinism(args.root)
        else:
            rendered, components = prepare_outputs(args.root)
            print(f"Validated {len(rendered['en'])} rules and {len(components['en'])} components")
    except (RuleError, OSError, ValueError) as exc:
        parser.exit(1, f"ERROR: {exc}\n")


if __name__ == "__main__":
    main()
