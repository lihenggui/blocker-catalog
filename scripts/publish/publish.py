#!/usr/bin/env python3
"""Publish the Blocker catalog to GitHub, GitLab, and the legacy endpoint."""

import argparse
import json
import os
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

# The dry run leaves even Python's bytecode cache untouched.
sys.dont_write_bytecode = True

from assets import (
    ASSETS, COMPONENTS, DATA_FILES, MANIFEST, RULES, PublishError, read_manifest, release_notes,
    unpack_data, verify_assets,
)
from legacy import publish_legacy
from providers import TAG, Commands, GitHub, GitLab

ROOT = Path(__file__).resolve().parents[2]


def build_command(commands, *args):
    return commands(sys.executable, "-B", "scripts/rules/build.py", *args,
                    operation="Build catalog" if "--check" not in args else "Check catalog build")


def build_release(commands, sequence, sha, commit_time):
    options = ("--sequence", str(sequence), "--source-commit", sha, "--published-at", commit_time)
    for args in (options, (*options, "--check")):
        result = build_command(commands, *args)
        print(result.stdout.decode().strip())


def publish(root, environ, *, dry_run=False, run=subprocess.run, open_url=None, now=lambda: datetime.now(UTC)):
    commands = Commands(root, environ, run)
    head = commands("git", "rev-parse", "HEAD", operation="Resolve checkout").stdout.decode().strip()
    sha = environ.get("GITHUB_SHA") or (head if dry_run else None)
    if sha != head:
        raise PublishError("GITHUB_SHA must identify the checked-out commit")
    raw_time = commands("git", "show", "-s", "--format=%cI", sha,
                        operation="Read source commit time").stdout.decode().strip()
    commit_time = datetime.fromisoformat(raw_time).astimezone(UTC).isoformat().replace("+00:00", "Z")

    gh_token = environ.get("GH_TOKEN") or environ.get("GITHUB_TOKEN")
    if not dry_run:
        for name, value in (("GH_TOKEN", gh_token), ("GITLAB_TOKEN", environ.get("GITLAB_TOKEN")),
                            ("LEGACY_DEPLOY_KEY", environ.get("LEGACY_DEPLOY_KEY")),
                            ("RUNNER_TEMP", environ.get("RUNNER_TEMP"))):
            if not value:
                raise PublishError(f"{name} is required to publish")

    github = GitHub(commands, gh_token)
    releases = github.releases()
    number = max((int(TAG.fullmatch(tag)[1]) for tag, release in releases.items()
                  if not release["draft"] and not release["prerelease"]), default=0)
    previous = releases.get(f"catalog-{number}")
    old_assets = github.assets(previous) if previous else {}
    old_manifest = read_manifest(github.download(old_assets, MANIFEST), number) if previous else None
    candidate = read_manifest(build_command(commands, "--manifest-only").stdout, 0)
    unchanged = old_manifest is not None and candidate["files"] == old_manifest["files"]
    sequence = number if unchanged else number + 1
    tag = f"catalog-{sequence}"
    action = "create a release"
    if unchanged:
        action = "reuse the published release"
    elif tag in releases:
        action = "resume the draft" if releases[tag]["draft"] else "verify the existing release"
    print(f"{'Dry run' if dry_run else 'Publish'} {tag} ({action})")
    print(f"Source commit {sha} at {commit_time}")
    if unchanged:
        print(f"Release tag source {old_manifest['sourceCommit']}")
    print("Destinations: GitHub release, GitLab mirror and release, legacy main")
    if dry_run:
        return

    build_release(commands, sequence, sha, commit_time)
    release_sha = sha
    if unchanged:
        release_sha = old_manifest["sourceCommit"]
        # Reproduce the published manifest for byte comparisons and mirror repair.
        if (release_sha, old_manifest["publishedAt"]) != (sha, commit_time):
            build_release(commands, sequence, release_sha, old_manifest["publishedAt"])

    expected = {name: (root / "out/release" / name).read_bytes() for name in ASSETS}
    manifest = verify_assets(expected, sequence)
    if manifest["files"] != candidate["files"]:
        raise PublishError("The real build differs from the placeholder build")
    if unchanged:
        notes = previous["body"] or ""
    else:
        old_data = {RULES: b'{"rules":[]}', COMPONENTS: b""}
        if previous:
            old_data = {name: unpack_data(old_manifest, name, github.download(old_assets, name)) for name in DATA_FILES}
        new_data = {name: unpack_data(manifest, name, expected[name]) for name in DATA_FILES}
        notes = release_notes(old_data, new_data)

    github.publish(tag, release_sha, notes, releases.get(tag), expected, sequence)
    with TemporaryDirectory(prefix="catalog-publish-", dir=environ["RUNNER_TEMP"]) as directory:
        gitlab = GitLab(commands, environ["GITLAB_TOKEN"], directory, open_url=open_url, now=now)
        gitlab.publish(tag, sequence, release_sha, notes, expected)
        publish_legacy(commands, root, directory, environ["LEGACY_DEPLOY_KEY"], sequence, sha)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="show the next publication without writing")
    args = parser.parse_args()
    try:
        publish(ROOT, os.environ, dry_run=args.dry_run)
    except (PublishError, KeyError, OSError, ValueError, subprocess.TimeoutExpired, zipfile.BadZipFile) as exc:
        # Exceptions from provider libraries may carry signed URLs or response data.
        message = str(exc) if isinstance(exc, PublishError) else f"Invalid or unavailable publication data ({type(exc).__name__})"
        parser.exit(1, f"ERROR: {message}\n")


if __name__ == "__main__":
    main()
