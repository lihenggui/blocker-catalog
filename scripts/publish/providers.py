"""Publish immutable catalog assets to GitHub and GitLab."""

import json
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from assets import ASSETS, PublishError, verify_assets

GITHUB_REPO = "lihenggui/blocker-catalog"
SOURCE_URL = f"https://github.com/{GITHUB_REPO}.git"
GITLAB_PROJECT = "https://gitlab.com/mercuryli/blocker-catalog"
GITLAB_API = "https://gitlab.com/api/v4/projects/89466134"
TAG = re.compile(r"catalog-([1-9][0-9]*)")


class Commands:
    def __init__(self, root, environ, run=subprocess.run):
        self.root = root
        self.run = run
        excluded = {"GH_TOKEN", "GITHUB_TOKEN", "GITLAB_TOKEN", "LEGACY_DEPLOY_KEY", "GH_DEBUG"}
        self.env = {key: value for key, value in environ.items()
                    if key not in excluded and not key.startswith("GIT_TRACE")}
        self.env.update(GIT_TERMINAL_PROMPT="0", GH_PROMPT_DISABLED="1")

    def __call__(self, *args, operation, cwd=None, env=None, data=None, allowed=(0,)):
        result = self.run(args, cwd=cwd or self.root, env=self.env | (env or {}),
                          input=data, capture_output=True, timeout=900)
        if result.returncode not in allowed:
            # Provider stderr can contain credentials or signed download URLs.
            raise PublishError(f"{operation} failed (exit {result.returncode})")
        return result


def require_bytes(expected, actual, location):
    if actual != expected:
        raise PublishError(f"{location} has different bytes")


class GitHub:
    def __init__(self, commands, token=None):
        self.commands = commands
        self.env = {"GH_TOKEN": token} if token else {}

    def api(self, method, path, *, data=None, content_type="application/json", accept=None, paginate=False):
        endpoint = path if path.startswith("https://") else f"repos/{GITHUB_REPO}/{path}"
        args = ["gh", "api", endpoint, "--hostname", "github.com", "--method", method]
        if paginate:
            args += ["--paginate", "--slurp"]
        if data is not None:
            args += ["--input", "-", "-H", f"Content-Type: {content_type}", "-H", f"Content-Length: {len(data)}"]
        if accept:
            args += ["-H", f"Accept: {accept}"]
        return self.commands(*args, env=self.env, data=data, operation=f"GitHub {method} {path}").stdout

    def releases(self):
        pages = json.loads(self.api("GET", "releases?per_page=100", paginate=True))
        return {row["tag_name"]: row for page in pages for row in page if TAG.fullmatch(row["tag_name"])}

    def assets(self, release):
        path = f"releases/{release['id']}/assets?per_page=100"
        pages = json.loads(self.api("GET", path, paginate=True))
        assets = {}
        for page in pages:
            for row in page:
                name = row["name"]
                if name in assets:
                    raise PublishError(f"GitHub release {release['tag_name']} has duplicate asset {name}")
                assets[name] = row
        return assets

    def download(self, assets, name):
        asset = assets.get(name)
        if asset is None or asset["state"] != "uploaded":
            raise PublishError(f"GitHub release is missing uploaded asset {name}")
        return self.api("GET", f"releases/assets/{asset['id']}", accept="application/octet-stream")

    def tag_commit(self, tag):
        ref = f"refs/tags/{tag}"
        result = self.commands("git", "ls-remote", "--tags", SOURCE_URL, ref, f"{ref}^{{}}",
                               operation=f"Read GitHub tag {tag}")
        refs = {name: sha for sha, name in (line.split() for line in result.stdout.decode().splitlines())}
        return refs.get(f"{ref}^{{}}", refs.get(ref))

    def publish(self, tag, sha, notes, release, expected, sequence):
        actual_sha = self.tag_commit(tag)
        if actual_sha != sha and (actual_sha is not None or (release and not release["draft"])):
            raise PublishError(f"GitHub tag {tag} points to {actual_sha or 'a missing ref'}, expected {sha}")
        if release is None:
            data = {"tag_name": tag, "target_commitish": sha, "name": f"Catalog {sequence}",
                    "body": notes, "draft": True}
            release = json.loads(self.api("POST", "releases", data=json.dumps(data).encode()))
        elif release["draft"] and release["target_commitish"] != sha:
            raise PublishError(f"GitHub draft {tag} targets {release['target_commitish']}, expected {sha}. "
                               "Rerun publication for the draft's source commit.")

        remote_assets = self.assets(release)
        if release["draft"]:
            for name, content_type in ASSETS.items():
                asset = remote_assets.get(name)
                # A failed GitHub upload can leave a disposable empty starter asset in a draft.
                if asset and asset["state"] == "starter":
                    self.api("DELETE", f"releases/assets/{asset['id']}")
                elif asset:
                    continue
                query = urlencode({"name": name})
                url = f"https://uploads.github.com/repos/{GITHUB_REPO}/releases/{release['id']}/assets?{query}"
                self.api("POST", url, data=expected[name], content_type=content_type)
            remote_assets = self.assets(release)

        downloaded = {}
        for name in ASSETS:
            downloaded[name] = self.download(remote_assets, name)
            require_bytes(expected[name], downloaded[name], f"GitHub {tag} asset {name}")
        verify_assets(downloaded, sequence)

        if release["draft"]:
            data = {"draft": False, "prerelease": False, "make_latest": "true"}
            self.api("PATCH", f"releases/{release['id']}", data=json.dumps(data).encode())


class GetRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if req.get_method() != "GET" or not newurl.startswith("https://"):
            # Turn an unexpected redirect into an HTTP error without replaying a write.
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class GitLab:
    def __init__(self, commands, token, temp_dir, *, now, open_url=None):
        self.commands = commands
        self.token = token
        self.temp_dir = temp_dir
        self.open_url = open_url if open_url is not None else build_opener(GetRedirects()).open
        self.now = now

    def request(self, method, url, *, data=None, content_type=None, public=False, missing=False):
        req = Request(url, data=data, method=method)
        if not public:
            # urllib forwards regular headers on redirects but strips unredirected ones.
            req.add_unredirected_header("PRIVATE-TOKEN", self.token)
        if content_type:
            req.add_header("Content-Type", content_type)
        expected = 200 if method == "GET" else 201
        try:
            with self.open_url(req, timeout=90) as response:
                if response.status != expected:
                    raise PublishError(f"GitLab {method} {url} returned HTTP {response.status}")
                return response.read()
        except HTTPError as exc:
            with exc:
                if exc.code == 404 and missing:
                    return None
                raise PublishError(f"GitLab {method} {url} returned HTTP {exc.code}") from None
        except (URLError, TimeoutError):
            raise PublishError(f"GitLab {method} {url} could not be reached") from None

    def push_refs(self, tag, sha):
        git = self.commands
        git("git", "fetch", "--no-tags", SOURCE_URL, "refs/heads/main", operation="Fetch GitHub main")
        main = git("git", "rev-parse", "FETCH_HEAD", operation="Resolve GitHub main").stdout.decode().strip()
        ref = f"refs/tags/{tag}"
        git("git", "fetch", "--no-tags", SOURCE_URL, ref, operation=f"Fetch GitHub tag {tag}")
        object_id, target = git("git", "rev-parse", "FETCH_HEAD", "FETCH_HEAD^{commit}",
                                operation=f"Resolve GitHub tag {tag}").stdout.decode().split()
        if target != sha:
            raise PublishError(f"GitHub tag {tag} points to {target}, expected {sha}")
        askpass = Path(self.temp_dir) / "gitlab-askpass"
        askpass.write_text('#!/bin/sh\nprintf "%s\\n" "$GITLAB_TOKEN"\n', encoding="utf-8")
        askpass.chmod(0o700)
        git("git", "-c", "credential.helper=", "-c", "credential.username=oauth2",
            "-c", "http.followRedirects=false", "push", "--atomic", f"{GITLAB_PROJECT}.git",
            f"+{main}:refs/heads/main", f"{object_id}:{ref}", operation="Push GitLab main and catalog tag",
            env={"GIT_ASKPASS": str(askpass), "GITLAB_TOKEN": self.token})

    def publish(self, tag, sequence, sha, notes, expected):
        self.push_refs(tag, sha)
        links = []
        for name, data in expected.items():
            url = f"{GITLAB_API}/packages/generic/catalog/{sequence}/{name}"
            existing = self.request("GET", url, missing=True)
            if existing is None:
                self.request("PUT", url, data=data, content_type="application/octet-stream")
                existing = self.request("GET", url)
            require_bytes(data, existing, f"GitLab package catalog/{sequence}/{name}")
            links.append({"name": name, "url": url, "direct_asset_path": f"/{name}", "link_type": "package"})

        releases = f"{GITLAB_API}/releases"
        if self.request("GET", f"{releases}/{tag}", missing=True) is None:
            latest = self.request("GET", f"{releases}/permalink/latest", missing=True)
            released_at = self.now()
            if latest is not None:
                previous = datetime.fromisoformat(json.loads(latest)["released_at"])
                released_at = max(released_at, previous + timedelta(microseconds=1))
            data = {"tag_name": tag, "name": f"Catalog {sequence}", "description": notes,
                    "released_at": released_at.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                    "assets": {"links": links}}
            self.request("POST", releases, data=json.dumps(data).encode(), content_type="application/json")

        for suffix in (tag, "permalink/latest"):
            for name, data in expected.items():
                url = f"{GITLAB_PROJECT}/-/releases/{suffix}/downloads/{name}"
                require_bytes(data, self.request("GET", url, public=True), f"GitLab {suffix} asset {name}")
