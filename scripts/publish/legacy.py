"""Update the legacy git endpoint from a checked build."""

import json
import os
import re
import shlex
import shutil
from pathlib import Path

from assets import PublishError

LEGACY_URL = "git@github.com:lihenggui/blocker-general-rules.git"
MIRROR_WORKFLOW = ".github/workflows/mirror-gitlab.yml"
# https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints
GITHUB_HOST_KEY = "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


def trim_mirror_workflow(workflow):
    workflow = re.sub(r"(?m)^  workflow_run:[^\n]*\n(?:^ {4,}[^\n]*\n)*", "", workflow)
    workflow = re.sub(r"(?m)^  # Bot commits pushed with GITHUB_TOKEN do not trigger push workflows[^\n]*\n",
                      "", workflow)
    return re.sub(r"(?m)^    if: github\.event_name != 'workflow_run' \|\| "
                  r"github\.event\.workflow_run\.conclusion == 'success'\r?\n", "", workflow)


def publish_legacy(commands, root, temp_dir, deploy_key, sequence, sha):
    temp_dir = Path(temp_dir)
    identity = temp_dir / "legacy-key"
    with os.fdopen(os.open(identity, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as key:
        key.write(deploy_key.rstrip() + "\n")
    known_hosts = temp_dir / "github-known-hosts"
    known_hosts.write_text(GITHUB_HOST_KEY + "\n", encoding="utf-8")
    ssh = shlex.join(["ssh", "-F", "/dev/null", "-i", str(identity), "-o", "BatchMode=yes",
                      "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
                      "-o", "StrictHostKeyChecking=accept-new", "-o", "HostKeyAlgorithms=ssh-ed25519",
                      "-o", f"UserKnownHostsFile={known_hosts}", "-o", "GlobalKnownHostsFile=/dev/null"])
    env = {"GIT_SSH_COMMAND": ssh}
    checkout = temp_dir / "legacy"
    commands("git", "clone", "--depth", "1", "--single-branch", "--branch", "main", LEGACY_URL,
             str(checkout), env=env, operation="Clone legacy main")
    license_data = (checkout / "LICENSE").read_bytes()
    mirror = trim_mirror_workflow((checkout / MIRROR_WORKFLOW).read_bytes().decode("utf-8"))
    for path in checkout.iterdir():
        if path.name == ".git":
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    for name in ("rules", "icon", "components"):
        shutil.copytree(root / "out/legacy" / name, checkout / name)
    shutil.copyfile(root / "legacy/endpoint/README.md", checkout / "README.md")
    (checkout / "LICENSE").write_bytes(license_data)
    (checkout / MIRROR_WORKFLOW).parent.mkdir(parents=True)
    (checkout / MIRROR_WORKFLOW).write_bytes(mirror.encode("utf-8"))

    for language in ("en", "zh-CN"):
        path = f"rules/{language}/general.json"
        try:
            rules = json.loads((checkout / path).read_bytes())
        except (OSError, ValueError):
            raise PublishError(f"Legacy {path} is missing or invalid JSON") from None
        if not isinstance(rules, list):
            raise PublishError(f"Legacy {path} must contain a JSON array")

    commands("git", "add", "--all", "--force", cwd=checkout, operation="Stage legacy endpoint")
    diff = commands("git", "diff", "--cached", "--quiet", cwd=checkout, allowed=(0, 1),
                    operation="Compare legacy endpoint tree")
    if diff.returncode == 0:
        return
    commands("git", "-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}",
             "commit", "-m", f"Publish catalog {sequence}", "-m", f"Source: lihenggui/blocker-catalog@{sha}",
             cwd=checkout, operation="Commit legacy endpoint")
    commands("git", "push", "origin", "HEAD:refs/heads/main", cwd=checkout, env=env,
             operation="Push legacy main")
