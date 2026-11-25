# Blocker catalog

This repository is the canonical catalog of Android SDK rules and component
descriptions used by Blocker. Manual edits, Exodus sync updates, crawler
evidence, and generated descriptions arrive through pull requests. The
`main` branch holds reviewed sources and evidence. Builds generate app data
under the gitignored `out/` directory.

Published app data comes from this repository's releases, and
`blocker-general-rules` remains the endpoint for older app versions.

## Repository layout

| Path | Contents |
| --- | --- |
| `data/rules/<slug>.yaml` | One SDK rule per file |
| `data/components/<class path>.yaml` | One component description and its metadata per file |
| `data/icons/` | App rule icons |
| `upstream/exodus/` | Trimmed tracker snapshot, reviewed exclusions, and link candidates |
| `evidence/prefixes.json` | Package-prefix attribution |
| `evidence/reports/` | Component coverage reports |
| `config/` | Repository seeds, SDK group overrides, and reviewed functional prefixes |
| `scripts/rules/` | Build, validation, Exodus sync and linking, and tests |
| `scripts/crawl/` | Resumable crawler, fixtures, and tests |
| `scripts/publish/` | Release publisher and tests |
| `.github/workflows/` | Validation, publishing, weekly crawler, and weekly Exodus sync |
| `legacy/` | Frozen English and Chinese components and their SHA-256 checksums |
| `out/legacy/` | Current `rules/<lang>/general.json`, frozen `components/<lang>/**/*.json`, and `icon/` |
| `out/v2/` | Uncompressed catalog, rules, components, and icons |
| `out/release/` | `v2-catalog.json`, `v2-rules.json.gz`, `v2-components.jsonl.gz`, and `v2-icons.zip` |

See [the v2 format](schema/v2.md) and [the frozen snapshot](legacy/README.md).

## Edit SDK rules

Use one YAML file per rule in `data/rules/`. Set a positive, unique app
`id`, local `keywords` (which may be empty), and an `i18n.en` mapping.
An optional `upstream.exodus` value links to the separate namespace of
Exodus tracker IDs. Keep assigned app IDs permanent. Allocate new IDs
above the maximum source ID and reserve retired IDs.

The legacy collision mappings are recorded in
`scripts/rules/migration_manifest.json`. Teemo moved from 35 to 468,
FidZup from 36 to 469, and Audience Studio (Krux) from 37 to 470.
Sohu keeps 465, and Honor keeps its published ID of 466. The two stray
records with ID 467 were removed after their network and website data
was retained under Gigya (168) and AppLovin (101). Keep 467 reserved.

Store `name`, `description`, `sideEffect`, and `company` under
`i18n.<lang>`. Use a nonempty rendered English name. Local English text
takes precedence over text from the linked tracker. The v2 client falls back
to English for each missing Chinese field. `safeToBlock` and `sideEffect` are
local decisions. Company names use their original translations. Exodus supplies
no company field. Language-neutral fields use the same values for both
languages. Where contributor names are combined, retain the English order
and append Chinese-only contributors.

The build merges upstream signatures before local signatures. Code keywords
are compared with trailing dots removed and local spellings take precedence.
For example, local `com.facebook.ads.` replaces upstream `com.facebook.ads`.
The app uses case-insensitive substring search for literal keywords, so a local
`com.google.android.gms.ads.` also suppresses an upstream
`com.google.android.gms.ads.AdView`. Local keywords are retained and
`excludeKeywords` uses the same normalization. Network signatures use exact
deduplication and exact `excludeNetworkSignatures`, including trailing dots.
Empty search signatures produce a validation warning. Legacy
`useRegexSearch: true` requires every rendered signature to compile.

## Edit component descriptions

Use one YAML file per fully qualified class. This example describes
`data/components/com/google/android/gms/ads/AdActivity.yaml`.

```yaml
recommendToBlock: false
contributor: Mercury
lastUpdateTime: '2023-11-19T16:45:01.937788900Z'
i18n:
  en:
    description: English component description
    disableEffect: Optional English effect
  zh-CN:
    description: 中文组件说明
```

`recommendToBlock` and `i18n.en.description` are required.
`contributor`, `lastUpdateTime`, `disableEffect`, and Chinese text are
optional. Use only the fields shown above. Quote `lastUpdateTime` to preserve
the ISO instant and fractional digits. Use Java identifiers for path segments,
including `$` for nested classes. The build derives `name` from the path.
It selects `sdkRuleId` by the longest case-insensitive literal keyword match,
with the lowest rule ID breaking ties. With no match, it omits `sdkRuleId`.
The v2 client falls back to English separately for each missing translation.
Explicit empty strings remain empty. Older apps receive the snapshot in
`legacy/`, which must remain unedited.

### Blocking recommendations

> **Recommend blocking only when disabling the component benefits the user (less tracking, ads,
> analytics, background wake-ups or battery use) and the app's main features keep working.** If
> disabling it can affect push notifications, sign-in, payments, the camera, language, maps, media
> playback, downloads the user started, accessibility, widgets the user placed, app start-up or
> library initialization, do not recommend it. When the available evidence does not establish both
> conditions, do not recommend it.

Use this definition for manual and generated entries. Default
`recommendToBlock` to `false` for metadata holders, startup providers,
`FileProvider`s, generic base classes, and shared job schedulers such as
WorkManager's system services and reschedule receiver. Check push and
messaging, authentication and OAuth, billing, camera and locale support,
user-facing location and media, downloads, accessibility, and home-screen
widgets for effects on those features.

Use `true` when the component's role and disable effect establish both
conditions. Examples include ad-only screens and download services,
analytics and install attribution, SDK-only wake-ups and telemetry uploads,
optional in-app marketing, and development-only previews, tests, and tracing.
Check whether crash reporting also supports recovery or reports users submit.

## Build and validate

Use Python 3.11 or newer. CI uses Python 3.12.

```sh
python3 -m pip install -r scripts/requirements.txt
python3 -m unittest discover -s scripts/rules -p 'test_*.py'
python3 -m unittest discover -s scripts/crawl -p 'test_*.py'
python3 -m unittest discover -s scripts/publish -p 'test_*.py'
python3 scripts/rules/build.py
python3 scripts/rules/build.py --check
python3 scripts/rules/validate.py --check
```

The offline build accepts `--sequence` (non-negative integer, default `0`),
`--source-commit` (default empty), and `--published-at` (UTC ISO instant
ending in `Z`, default `1970-01-01T00:00:00Z`). Use the same options with
`build.py --check` to verify the existing outputs without writing to them.
`validate.py` checks sources and frozen checksums. `validate.py --check` also
compares two clean builds byte for byte and verifies the v2 catalog.
Validation runs on pull requests and pushes to `main`.

## Publishing

The **Publish catalog** workflow verifies the build, publishes changed
catalog data to GitHub and GitLab as `catalog-<sequence>` (title
`Catalog <sequence>`), and updates the legacy `blocker-general-rules`
endpoint. Release assets use the `v<format>-` prefix. If catalog files
are unchanged, the workflow checks the existing release and completes
any pending mirror or legacy updates. Published releases are immutable.
A correction is a new release with a higher sequence.

Run the workflow manually for the first publication. Set the repository
variable `PUBLISH_ENABLED` to `true` to also publish on pushes to `main`.
Use `python3 scripts/publish/publish.py --dry-run` to check the next
publication without writing files or changing either provider.

Format 2 clients fetch the manifest from
[GitHub](https://github.com/lihenggui/blocker-catalog/releases/latest/download/v2-catalog.json)
or [GitLab](https://gitlab.com/mercuryli/blocker-catalog/-/releases/permalink/latest/downloads/v2-catalog.json).

## Exodus updates

The weekly and manually dispatched workflow fetches one snapshot, links
existing rules conservatively, and imports eligible unlinked trackers. It
validates the result and opens or updates a pull request from
`automation/sync-exodus`. The tracker endpoint is public and needs no API
key. The fetch step sends `EXODUS_API_TOKEN` only when that secret exists.

```sh
python3 scripts/rules/sync_exodus.py --snapshot-only --summary-file .local/exodus-fetch.md
python3 scripts/rules/link_exodus.py --dry-run
python3 scripts/rules/link_exodus.py --summary-file .local/exodus-links.md
python3 scripts/rules/sync_exodus.py --from-snapshot --summary-file .local/exodus-sync.md
python3 scripts/rules/validate.py --check
```

The snapshot keeps tracker IDs, names, code and network signatures, websites,
descriptions, and categories, sorted by tracker ID. A failed fetch leaves the
previous snapshot intact. `--from-snapshot` works offline, and `--input FILE`
accepts a saved API response for local testing.

Automatic linking requires a unique, reciprocal keyword match with an exact
match after stripping trailing dots. Review prefix-only and conflicting
matches in `upstream/exodus/link_candidates.md`. Sync also checks for
case-insensitive substring overlap in either direction after stripping leading
and trailing dots. It lists overlaps for manual linking and allocates fresh
app IDs only to eligible trackers with nonempty code signatures.

Use `upstream/exodus/ignore.yaml` to record reviewed exclusions as
`{id, reason}` entries. Exodus tracker 363 (Pangle) is excluded because its
signature covers the ByteDance namespace. Keep the specific reviewed local
Pangle keywords. Review new rules for translations and safety before merging.
Linking preserves app IDs and local text and safety decisions. If an upstream
tracker disappears, the rule retains its local data and validation warns.

## Crawler evidence

The weekly and manually dispatched crawler runs these stages.

1. `discover` collects built-in repositories (Google Maven and Maven Central),
   configured seeds, repository URLs from Android Gradle builds found on
   GitHub, and repositories declared by fetched POMs.
2. `probe` identifies listing capabilities, including Google and Central
   indexes, Nexus, Artifactory, directory listing, and metadata-only access.
   It skips proxy mirrors of Maven Central.
3. `enumerate` lists artifacts or queries known coordinates where repository
   listings are unavailable.
4. `fetch` and `extract` read AAR manifests with HTTP range requests when
   available and record component declarations per artifact.
5. `group`, `prefixes`, and `report` prepare SDK coverage, attribute package
   prefixes to rules, and report uncovered or co-occurring prefixes using
   the catalog's rendered keywords. Record reviewed functional prefixes in
   `config/functional.yaml` to remove them from the review lists. Add
   reviewed tracker keywords to the corresponding files in `data/rules/`.

The crawler saves manifest extracts in `artifacts/`, discovered repositories
in `repositories.json`, and progress in `state/progress.json` under
`--state-dir`. It stops at `--max-minutes` and resumes from the saved state.
The workflow keeps the latest checkpoint as a single commit on `crawl-state`
and caches extracted manifests in `.cache/manifests/`. Evidence changes open
or update a pull request from `automation/crawl`.

For a local run, use `.crawl-state/` or pass a separate state path. To
resume the published checkpoint, check out `crawl-state` into that directory
before running the crawler.

```sh
python3 scripts/crawl/crawl.py all --max-minutes 60
```

GitHub code search uses `CODE_SEARCH_TOKEN` when configured and falls back to
`GITHUB_TOKEN`. Both automation workflows use the repository setting that
allows GitHub Actions to create pull requests.
