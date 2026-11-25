import gzip
import hashlib
import io
import json
import shlex
import stat
import subprocess
import unittest
import zipfile
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

from assets import ASSETS, COMPONENTS, ICONS, MANIFEST, RULES, PublishError, release_notes, verify_assets
from legacy import BOT_EMAIL, BOT_NAME, GITHUB_HOST_KEY, LEGACY_URL, MIRROR_WORKFLOW
from providers import GITHUB_REPO, GITLAB_API, GITLAB_PROJECT, SOURCE_URL, GetRedirects
from publish import publish

SOURCE = "a" * 40
NEXT = "b" * 40
MAIN = "c" * 40
TAG_OBJECT = "d" * 40
NOW = datetime(2026, 11, 25, 21, 0, tzinfo=UTC)
COMMIT_TIME = "2026-11-25T20:30:00Z"
MIRROR = """name: Mirror to GitLab

on:
  push:
    branches: [main]
  # Bot commits pushed with GITHUB_TOKEN do not trigger push workflows, so mirror again after regeneration.
  workflow_run:
    workflows: [Regenerate app data]
    types: [completed]
  workflow_dispatch:

jobs:
  mirror:
    if: github.event_name != 'workflow_run' || github.event.workflow_run.conclusion == 'success'
    runs-on: ubuntu-latest
"""
TRIMMED_MIRROR = """name: Mirror to GitLab

on:
  push:
    branches: [main]
  workflow_dispatch:

jobs:
  mirror:
    runs-on: ubuntu-latest
"""


def js(value):
    return json.dumps(value).encode()


def make_assets(sequence, sha, commit_time, keyword="old"):
    rules = js({"rules": [{"id": 7, "keywords": [keyword], "i18n": {"en": {"name": "Example SDK"}}}]})
    components = js({"name": "example.Service", "recommendToBlock": False, "i18n": {"en": {"description": "Service"}}})
    contents = {"components.jsonl": components, "icon/example.svg": b"<svg/>", "rules.json": rules}
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as icons:
        icons.writestr(zipfile.ZipInfo("icon/example.svg"), contents["icon/example.svg"])
    return {
        MANIFEST: js({"format": 2, "sequence": sequence, "sourceCommit": sha, "publishedAt": commit_time,
                      "files": [{"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                                for name, data in contents.items()]}),
        RULES: gzip.compress(rules, mtime=0),
        COMPONENTS: gzip.compress(components, mtime=0),
        ICONS: archive.getvalue(),
    }


class Response(io.BytesIO):
    def __init__(self, status, data):
        super().__init__(data)
        self.status = status


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "legacy/endpoint").mkdir(parents=True)
        (self.root / "legacy/endpoint/README.md").write_bytes(b"Endpoint documentation\n")
        self.env = {"GITHUB_SHA": SOURCE, "GH_TOKEN": "github-secret", "GITLAB_TOKEN": "gitlab-secret",
                    "LEGACY_DEPLOY_KEY": "legacy-secret\n", "RUNNER_TEMP": str(self.root)}
        self.keyword = "old"
        self.builds = []
        self.writes = []
        self.github_releases = {}
        self.github_tags = {}
        self.github_assets = {}
        self.downloads = []
        self.packages = {}
        self.gitlab_releases = {}
        self.legacy_tree = {
            "README.md": b"Old documentation",
            "LICENSE": b"Legacy license",
            MIRROR_WORKFLOW: MIRROR.encode(),
            "src/old.yaml": b"Old source",
            "scripts/build.py": b"Old script",
            "upstream/trackers.json": b"Old upstream",
            "component-details/old.json": b"Old component",
            "crowdin.yml": b"Old configuration",
            ".github/workflows/regenerate.yml": b"Old workflow",
        }
        self.fetched = None
        self.failed_upload = None
        self.unavailable_package = None
        self.bad_legacy = None
        self.legacy_rules = b"[]"
        self.bad_permalink = False
        self.output = io.StringIO()

    def invoke(self, dry_run=False):
        with redirect_stdout(self.output):
            publish(self.root, self.env, dry_run=dry_run, run=self.run_command, open_url=self.open_url,
                    now=lambda: NOW)

    def run_command(self, args, *, cwd, env, input, capture_output, timeout):
        self.assertTrue(capture_output)
        self.assertGreater(timeout, 0)
        self.assertNotIn("LEGACY_DEPLOY_KEY", env)
        self.assertTrue(all("-secret" not in arg for arg in args))

        def result(stdout=b"", code=0):
            return subprocess.CompletedProcess(args, code, stdout, b"redacted")

        if args[0] == "gh":
            self.assertEqual(env["GH_TOKEN"], self.env["GH_TOKEN"])
            self.assertNotIn("GITLAB_TOKEN", env)
            if input is not None:
                self.assertIn(f"Content-Length: {len(input)}", args)
            stdout, code = self.run_github(args, input)
            return result(stdout, code)
        self.assertNotIn("GH_TOKEN", env)

        if args[0] != "git":
            self.assertEqual(args[1:3], ("-B", "scripts/rules/build.py"))
            if "--manifest-only" in args:
                self.builds.append(("preview",))
                return result(make_assets(0, "", COMMIT_TIME, self.keyword)[MANIFEST])
            sequence, sha, timestamp = int(args[4]), args[6], args[8]
            self.assertEqual(args[3:8:2], ("--sequence", "--source-commit", "--published-at"))
            assets = make_assets(sequence, sha, timestamp, self.keyword)
            out = self.root / "out"
            checking = "--check" in args
            self.builds.append(("check" if checking else "build", sequence, sha, timestamp))
            for name, data in assets.items():
                file = out / "release" / name
                if checking:
                    self.assertEqual(file.read_bytes(), data)
                else:
                    self.write_file(out, f"release/{name}", data)
            if not checking:
                self.write_file(out, "legacy/rules/en/general.json", self.legacy_rules)
                if self.bad_legacy != "missing":
                    data = b"malformed" if self.bad_legacy == "invalid" else b"[]"
                    self.write_file(out, "legacy/rules/zh-CN/general.json", data)
                self.write_file(out, "legacy/icon/example.svg", b"<svg/>")
                self.write_file(out, "legacy/components/en/example.json", b"{}")
            return result(b"Catalog build output")

        git = args[1:]
        if git == ("rev-parse", "HEAD"):
            return result(self.env["GITHUB_SHA"].encode())
        if git[:3] == ("show", "-s", "--format=%cI"):
            self.assertEqual(git[3], self.env["GITHUB_SHA"])
            return result(b"2026-11-25T12:30:00-08:00")
        if git[:3] == ("ls-remote", "--tags", SOURCE_URL):
            tag = git[3].removeprefix("refs/tags/")
            if tag not in self.github_tags:
                return result()
            return result(f"{TAG_OBJECT}\t{git[3]}\n{self.github_tags[tag]}\t{git[4]}\n".encode())
        if git[:3] == ("fetch", "--no-tags", SOURCE_URL):
            self.fetched = git[3]
            return result()
        if git == ("rev-parse", "FETCH_HEAD"):
            self.assertEqual(self.fetched, "refs/heads/main")
            return result(MAIN.encode())
        if git == ("rev-parse", "FETCH_HEAD", "FETCH_HEAD^{commit}"):
            tag = self.fetched.removeprefix("refs/tags/")
            return result(f"{TAG_OBJECT}\n{self.github_tags[tag]}\n".encode())
        if git[:7] == ("-c", "credential.helper=", "-c", "credential.username=oauth2",
                       "-c", "http.followRedirects=false", "push"):
            self.assertEqual(git[7:10], ("--atomic", f"{GITLAB_PROJECT}.git", f"+{MAIN}:refs/heads/main"))
            self.assertEqual(git[10], f"{TAG_OBJECT}:{self.fetched}")
            askpass = Path(env["GIT_ASKPASS"])
            self.assertIn('"$GITLAB_TOKEN"', askpass.read_text())
            self.assertNotIn(self.env["GITLAB_TOKEN"], askpass.read_text())
            self.assertEqual(env["GITLAB_TOKEN"], self.env["GITLAB_TOKEN"])
            self.writes.append(("gitlab", "push"))
            return result()
        self.assertNotIn("GITLAB_TOKEN", env)
        if git[:7] == ("clone", "--depth", "1", "--single-branch", "--branch", "main", LEGACY_URL):
            parts = shlex.split(env["GIT_SSH_COMMAND"])
            key = Path(parts[parts.index("-i") + 1])
            self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
            self.assertEqual(key.read_text(), self.env["LEGACY_DEPLOY_KEY"])
            self.assertIn("StrictHostKeyChecking=accept-new", parts)
            known_hosts = next(part.split("=", 1)[1] for part in parts if part.startswith("UserKnownHostsFile="))
            self.assertEqual(Path(known_hosts).read_text().strip(), GITHUB_HOST_KEY)
            checkout = Path(git[7])
            (checkout / ".git").mkdir(parents=True)
            for name, data in self.legacy_tree.items():
                self.write_file(checkout, name, data)
            return result()
        if git == ("add", "--all", "--force"):
            return result()
        if git == ("diff", "--cached", "--quiet"):
            return result(code=int(self.tree(cwd) != self.legacy_tree))
        if git[:5] == ("-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}", "commit"):
            self.assertEqual(git[7:], ("-m", f"Source: {GITHUB_REPO}@{self.env['GITHUB_SHA']}"))
            self.writes.append(("legacy", git[6]))
            return result()
        if git == ("push", "origin", "HEAD:refs/heads/main"):
            self.assertIn("GIT_SSH_COMMAND", env)
            self.legacy_tree = self.tree(cwd)
            self.writes.append(("legacy", "push"))
            return result()
        self.fail(f"Unexpected command {args}")

    @staticmethod
    def write_file(root, name, data):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    @staticmethod
    def tree(root):
        return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    def run_github(self, args, payload):
        url = urlsplit(args[2])
        path = url.path.removeprefix(f"/repos/{GITHUB_REPO}/").removeprefix(f"repos/{GITHUB_REPO}/")
        method = args[args.index("--method") + 1]
        if method != "GET":
            self.writes.append(("github", method, path))
        if path == "releases" and method == "GET":
            self.assertIn("--paginate", args)
            self.assertIn("--slurp", args)
            return js([list(self.github_releases.values())[:1], list(self.github_releases.values())[1:]]), 0
        if path == "releases" and method == "POST":
            release = self.add_github_release(json.loads(payload))
            self.assertTrue(release["draft"])
            return js(release), 0
        if path.startswith("releases/assets/"):
            assets, name, row = next((assets, name, asset) for assets in self.github_assets.values()
                                     for name, asset in assets.items() if asset["id"] == int(path.rsplit("/", 1)[1]))
            if method == "DELETE":
                self.assertEqual(row["state"], "starter")
                del assets[name]
                return b"", 0
            self.assertIn("Accept: application/octet-stream", args)
            self.downloads.append(name)
            return row["bytes"], 0
        release = next(row for row in self.github_releases.values() if row["id"] == int(path.split("/")[1]))
        tag = release["tag_name"]
        assets = self.github_assets[tag]
        if path.endswith("/assets") and method == "GET":
            return js([[{k: row[k] for k in ("id", "name", "state")} for row in assets.values()]]), 0
        if path.endswith("/assets") and method == "POST":
            self.assertEqual(url.netloc, "uploads.github.com")
            name = parse_qs(url.query)["name"][0]
            self.assertIn(f"Content-Type: {ASSETS[name]}", args)
            self.assertTrue(release["draft"])
            self.assertNotIn(name, assets)
            failed = name == self.failed_upload
            self.add_github_asset(tag, name, b"" if failed else payload, "starter" if failed else "uploaded")
            return b"", int(failed)
        if method == "PATCH":
            self.assertEqual(json.loads(payload), {"draft": False, "prerelease": False, "make_latest": "true"})
            self.assertEqual(set(self.downloads[-4:]), set(ASSETS))
            release["draft"] = False
            self.github_tags[tag] = release["target_commitish"]
            return js(release), 0
        self.fail(f"Unexpected GitHub request {method} {path}")

    def add_github_release(self, data):
        tag = data["tag_name"]
        release = {"id": len(self.github_releases) + 1, "prerelease": False, **data}
        self.github_releases[tag] = release
        self.github_assets[tag] = {}
        if not release["draft"]:
            self.github_tags[tag] = release["target_commitish"]
        return release

    def add_github_asset(self, tag, name, data, state="uploaded"):
        ids = [row["id"] for assets in self.github_assets.values() for row in assets.values()]
        self.github_assets[tag][name] = {"id": max(ids, default=0) + 1, "name": name, "state": state, "bytes": data}

    def open_url(self, req, *, timeout):
        self.assertGreater(timeout, 0)
        method, url = req.get_method(), req.full_url
        releases_url = f"{GITLAB_API}/releases"
        if url.startswith(GITLAB_API):
            self.assertEqual(dict(req.unredirected_hdrs), {"Private-token": self.env["GITLAB_TOKEN"]})
        else:
            self.assertEqual(req.header_items(), [])
        if "/packages/generic/catalog/" in url:
            if method == "PUT":
                name = url.rsplit("/", 1)[1]
                if name == self.unavailable_package:
                    raise HTTPError(url, 502, "Unavailable", {}, None)
                self.assertNotIn(url, self.packages)
                self.packages[url] = req.data
                self.writes.append(("gitlab", "PUT", name))
                return Response(201, b"{}")
            if url in self.packages:
                return Response(200, self.packages[url])
        if url == releases_url and method == "POST":
            release = json.loads(req.data)
            tag = release["tag_name"]
            self.assertNotIn(tag, self.gitlab_releases)
            self.assertEqual(release["name"], f"Catalog {tag.split('-')[1]}")
            self.assertTrue(release["released_at"].endswith("Z"))
            self.assertEqual({link["direct_asset_path"] for link in release["assets"]["links"]},
                             {f"/{name}" for name in ASSETS})
            self.gitlab_releases[tag] = release
            self.writes.append(("gitlab", "release", tag))
            return Response(201, js(release))
        latest = max(self.gitlab_releases.values(), default=None,
                     key=lambda release: datetime.fromisoformat(release["released_at"]))
        if url.startswith(releases_url + "/"):
            suffix = url.removeprefix(releases_url + "/")
            release = latest if suffix == "permalink/latest" else self.gitlab_releases.get(suffix)
            if release:
                return Response(200, js(release))
        if url.startswith(GITLAB_PROJECT + "/-/releases/"):
            tag, name = url.removeprefix(GITLAB_PROJECT + "/-/releases/").split("/downloads/")
            release = latest if tag == "permalink/latest" else self.gitlab_releases[tag]
            link = next(item for item in release["assets"]["links"] if item["name"] == name)
            data = b"broken permalink" if self.bad_permalink else self.packages[link["url"]]
            return Response(200, data)
        raise HTTPError(url, 404, "Missing", {}, None)

    def test_given_no_release_when_dry_run_then_it_shows_sequence_one_without_writes(self):
        self.invoke(dry_run=True)
        self.assertIn("Dry run catalog-1 (create a release)", self.output.getvalue())
        self.assertIn(COMMIT_TIME, self.output.getvalue())
        self.assertEqual(self.builds, [("preview",)])
        self.assertEqual(self.writes, [])
        self.assertFalse((self.root / "out").exists())

    def test_given_a_starter_asset_when_rerun_then_the_draft_is_completed_before_mirroring(self):
        self.failed_upload = RULES
        with self.assertRaisesRegex(PublishError, "GitHub POST .* failed"):
            self.invoke()
        self.assertTrue(self.github_releases["catalog-1"]["draft"])
        self.assertFalse(any(write[0] != "github" for write in self.writes))
        self.writes.clear()
        self.failed_upload = None

        self.invoke()
        self.assertFalse(self.github_releases["catalog-1"]["draft"])
        self.assertNotIn(("github", "POST", "releases"), self.writes)
        self.assertEqual(sum(write[:2] == ("github", "DELETE") for write in self.writes), 1)
        self.assertEqual(sum(write[:2] == ("github", "POST") for write in self.writes), 3)
        self.assertLess(self.writes.index(("github", "PATCH", "releases/1")), self.writes.index(("gitlab", "push")))
        self.assertLess(self.writes.index(("gitlab", "release", "catalog-1")),
                        self.writes.index(("legacy", "Publish catalog 1")))
        self.assertEqual(self.legacy_tree, {
            "README.md": b"Endpoint documentation\n", "LICENSE": b"Legacy license",
            MIRROR_WORKFLOW: TRIMMED_MIRROR.encode(), "rules/en/general.json": b"[]",
            "rules/zh-CN/general.json": b"[]", "icon/example.svg": b"<svg/>",
            "components/en/example.json": b"{}",
        })

        self.writes.clear()
        self.invoke()
        self.assertEqual(self.writes, [("gitlab", "push")])

    def test_given_an_incomplete_mirror_when_unchanged_sources_run_then_the_original_release_is_completed(self):
        self.unavailable_package = COMPONENTS
        with self.assertRaisesRegex(PublishError, "returned HTTP 502"):
            self.invoke()
        self.assertEqual(len(self.packages), 2)
        self.assertEqual(self.gitlab_releases, {})
        original_notes = self.github_releases["catalog-1"]["body"]
        self.writes.clear()
        self.env["GITHUB_SHA"] = NEXT
        self.unavailable_package = None

        self.invoke()
        self.assertEqual(self.builds[-4:], [
            ("build", 1, NEXT, COMMIT_TIME), ("check", 1, NEXT, COMMIT_TIME),
            ("build", 1, SOURCE, COMMIT_TIME), ("check", 1, SOURCE, COMMIT_TIME),
        ])
        self.assertFalse(any(write[0] == "github" for write in self.writes))
        self.assertEqual([write[2] for write in self.writes if write[:2] == ("gitlab", "PUT")], [COMPONENTS, ICONS])
        self.assertEqual(self.gitlab_releases["catalog-1"]["description"], original_notes)
        self.assertEqual(self.writes[-1], ("legacy", "push"))

    def test_given_changed_files_when_published_then_the_next_release_summarizes_the_previous_payload(self):
        self.invoke()
        self.add_github_release({"tag_name": "unrelated", "target_commitish": SOURCE, "draft": False})
        self.add_github_release({"tag_name": "catalog-72", "target_commitish": SOURCE, "draft": True})
        self.env["GITHUB_SHA"] = NEXT
        self.keyword = "new"
        self.invoke()
        release = self.github_releases["catalog-2"]
        self.assertEqual(release["target_commitish"], NEXT)
        self.assertIn("7 · Example SDK (+ new, − old)", release["body"])
        self.assertEqual(self.gitlab_releases["catalog-2"]["released_at"], "2026-11-25T21:00:00.000001Z")

    def test_given_only_legacy_files_changed_when_published_then_only_mirror_refs_and_legacy_are_pushed(self):
        self.invoke()
        self.writes.clear()
        self.env["GITHUB_SHA"] = NEXT
        self.legacy_rules = b'[{"id":7}]'

        self.invoke()

        self.assertEqual(self.writes, [("gitlab", "push"), ("legacy", "Publish catalog 1"), ("legacy", "push")])
        self.assertEqual(self.legacy_tree["rules/en/general.json"], self.legacy_rules)

    def test_given_published_assets_with_different_bytes_when_rerun_then_it_fails_without_any_writes(self):
        self.invoke()
        self.github_assets["catalog-1"][ICONS]["bytes"] = b"bad archive"
        self.writes.clear()
        with self.assertRaisesRegex(PublishError, "GitHub catalog-1 asset v2-icons.zip has different bytes"):
            self.invoke()
        self.assertEqual(self.writes, [])

    def test_given_a_gitlab_package_conflict_when_rerun_then_it_does_not_overwrite_or_update_legacy(self):
        self.invoke()
        self.packages[f"{GITLAB_API}/packages/generic/catalog/1/{MANIFEST}"] = b"wrong bytes"
        self.writes.clear()
        with self.assertRaisesRegex(PublishError, "GitLab package catalog/1/v2-catalog.json has different bytes"):
            self.invoke()
        self.assertEqual(self.writes, [("gitlab", "push")])

    def test_given_a_broken_public_permalink_when_verified_then_it_stops_before_legacy(self):
        self.bad_permalink = True
        with self.assertRaisesRegex(PublishError, "GitLab catalog-1 asset .* has different bytes"):
            self.invoke()
        self.assertFalse(any(write[0] == "legacy" for write in self.writes))

    def test_given_legacy_rules_missing_or_malformed_when_publishing_then_no_legacy_commit_is_made(self):
        for state in ("missing", "invalid"):
            with self.subTest(state=state):
                self.bad_legacy = state
                with self.assertRaisesRegex(PublishError, "rules/zh-CN/general.json is missing or invalid JSON"):
                    self.invoke()
                self.assertFalse(any(write[0] == "legacy" for write in self.writes))


class AssetTests(unittest.TestCase):
    def test_given_wrong_uncompressed_bytes_when_verified_then_the_manifest_check_fails(self):
        assets = make_assets(1, SOURCE, COMMIT_TIME)
        assets[RULES] = gzip.compress(b"wrong")
        with self.assertRaisesRegex(PublishError, "rules.json does not match the manifest"):
            verify_assets(assets, 1)

    def test_given_text_and_recommendation_changes_when_summarized_then_notes_count_and_cap_them(self):
        def rule(number, name, keywords):
            return {"id": number, "i18n": {"en": {"name": name}}, "keywords": keywords}

        def component(name, recommended, description, updated):
            return {"name": name, "recommendToBlock": recommended, "updatedAt": updated,
                    "i18n": {"en": {"description": description}}}

        before = [
            component("example.Service", False, "old", "2025"),
            component("example.Ad", False, "text", "2025"),
            component("example.Removed", False, "old", "2025"),
        ]
        after = [
            component("example.Service", False, "new", "2026"),
            component("example.Ad", True, "text", "2025"),
            component("example.Added", False, "text", "2026"),
        ]
        notes = release_notes(
            {RULES: js({"rules": [rule(1, "Original", []), rule(2, "Old", ["old"]), rule(3, "Removed", [])]}),
             COMPONENTS: b"\n".join(map(js, before))},
            {RULES: js({"rules": [rule(1, "Renamed", []), rule(2, "New", [str(n) for n in range(10)]),
                                 *(rule(n, "Added", []) for n in range(4, 14))]}),
             COMPONENTS: b"\n".join(map(js, after))},
        )
        for expected in ("Rules added (10)", "2 more", "3 · Removed", "1 · Renamed",
                         "4 more (10 total)", "− old", "Components removed (1)", "Components added (1)",
                         "example.Ad (false → true)", "Text-only changes (1 rules, 1 components)"):
            self.assertIn(expected, notes)

    def test_given_a_gitlab_redirect_when_followed_then_the_private_token_is_stripped(self):
        url = f"{GITLAB_API}/packages/generic/catalog/1/{MANIFEST}"
        req = Request(url)
        req.add_unredirected_header("PRIVATE-TOKEN", "secret")
        handler = GetRedirects()
        redirected = handler.redirect_request(req, None, 302, "", {}, "https://storage.example.com/asset")
        self.assertIsNone(redirected.get_header("Private-token"))
        self.assertIsNone(handler.redirect_request(req, None, 302, "", {}, "http://storage.example.com/asset"))
        self.assertIsNone(handler.redirect_request(Request(url, method="PUT"), None, 302, "", {},
                                                    "https://storage.example.com/asset"))


if __name__ == "__main__":
    unittest.main()
