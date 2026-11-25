"""Offline regression tests for compatibility, conservative links, and sync."""

import contextlib
import copy
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error
import zipfile

import rulelib
from build import build, catalog_files
from compare_legacy import compare, differences, summarize
from exoduslib import link_candidates, prefix_overlap
from link_exodus import link
from migrate_components import migrate_components
from migrate_from_json import migrate, pair_languages
from rulelib import (
    ROOT, RuleError, check_rules, component_json_text, component_name, json_text,
    load_component_sources, load_snapshot, load_sources,
    load_ignored, merge_keywords, merge_signatures, prepared, render_all, render_component,
    render_components, render_rule, trim_trackers, write_text, yaml_text,
)
from sync_exodus import EXODUS_URL, fetch_trackers, sync
from validate import check_determinism, check_v2


def tracker(tracker_id, name, code, network=""):
    return {
        "id": tracker_id, "name": name, "code_signature": code,
        "network_signature": network, "website": "https://example.test/",
        "description": "Upstream description", "categories": ["Analytics"],
    }


def source(rule_id=10, name="Local", keywords=None):
    return {
        "id": rule_id, "keywords": keywords if keywords is not None else ["com.local."],
        "i18n": {"en": {"name": name, "description": "Local description"}},
    }


def component_source():
    return {"recommendToBlock": False, "i18n": {"en": {"description": "English description"}}}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".local").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="pipeline-test-", dir=ROOT / ".local")
        self.root = Path(self.temp.name)
        self.out = self.root / "out"
        write_text(self.root / "legacy/components.sha256", "")
        self.quiet_out = contextlib.redirect_stdout(io.StringIO())
        self.quiet_err = contextlib.redirect_stderr(io.StringIO())
        self.quiet_out.__enter__()
        self.quiet_err.__enter__()

    def tearDown(self):
        self.quiet_out.__exit__(None, None, None)
        self.quiet_err.__exit__(None, None, None)
        self.temp.cleanup()

    def sources(self, *rules):
        for index, rule in enumerate(rules):
            write_text(self.root / f"data/rules/rule-{index}.yaml", yaml_text(rule))
        return load_sources(self.root)

    def snapshot(self, *trackers):
        write_text(self.root / "upstream/exodus/trackers.json",
                   json_text(sorted(trackers, key=lambda t: t["id"])))

    def ignore(self, *entries):
        write_text(self.root / "upstream/exodus/ignore.yaml", yaml_text(list(entries)))

    def components(self, *entries):
        for path, component in entries:
            write_text(self.root / "data/components" / path, yaml_text(component))
        return load_component_sources(self.root)

    def v2_components(self):
        return [json.loads(line) for line in (self.out / "v2/components.jsonl").read_bytes().splitlines()]

    def test_v2_rules_share_legacy_keywords_and_keep_only_defined_translations(self):
        linked = source(8, "Local", ["com.vendor.", "com.local"])
        linked.update(upstream={"exodus": 99}, excludeKeywords=["com.drop."], website="",
                      iconUrl="icon/sample.svg", safeToBlock=False, contributors=["Mercury"])
        linked["i18n"]["en"] = {"name": "Local", "company": "Example", "sideEffect": ""}
        linked["i18n"]["zh-CN"] = {"description": "中文说明"}
        self.sources(linked, dict(source(2, "Empty", []), iconUrl=""))
        self.snapshot(tracker(99, "Upstream", "com.remote|com.vendor|com.drop", "example"))
        write_text(self.root / "data/icons/sample.svg", "<svg/>")
        build(self.root)
        raw = (self.out / "v2/rules.json").read_bytes()
        self.assertIn("中文说明".encode(), raw)
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(json.loads(raw), {"rules": [
            {"id": 2, "keywords": [], "contributors": [],
             "i18n": {"en": {"name": "Empty", "description": "Local description"}}},
            {"id": 8, "icon": "icon/sample.svg", "website": "",
             "keywords": ["com.remote", "com.vendor.", "com.local"], "safeToBlock": False,
             "contributors": ["Mercury"],
             "i18n": {"en": {"name": "Local", "company": "Example",
                             "description": "Upstream description", "sideEffect": ""},
                      "zh-CN": {"description": "中文说明"}}},
        ]})

    def test_v2_component_lines_are_sorted_compact_and_preserve_optional_fields(self):
        self.sources(source(keywords=["com.vendor"]))
        component = component_source()
        component.update(contributor="Mercury", lastUpdateTime="2023-11-19T16:45:01.937788900Z")
        component["i18n"]["en"].update(description="Line one\nline two", disableEffect="English effect")
        component["i18n"]["zh-CN"] = {"description": "中文说明", "disableEffect": ""}
        self.components(("z/Last.yaml", component_source()), ("com/vendor/Service.yaml", component))
        build(self.root)
        path = self.out / "v2/components.jsonl"
        expected = (
            '{"name":"com.vendor.Service","sdkRuleId":10,"recommendToBlock":false,"contributor":"Mercury",'
            '"updatedAt":"2023-11-19T16:45:01.937788900Z","i18n":{"en":{"description":"Line one\\nline two",'
            '"disableEffect":"English effect"},"zh-CN":{"description":"中文说明","disableEffect":""}}}\n'
            '{"name":"z.Last","recommendToBlock":false,"i18n":{"en":{"description":"English description"}}}\n'
        ).encode()
        self.assertEqual(path.read_bytes(), expected)
        mtime = path.stat().st_mtime_ns
        build(self.root)
        self.assertEqual(path.stat().st_mtime_ns, mtime)
        build(self.root, check=True)

    def test_v2_omits_fallback_text_for_partial_and_missing_chinese_translations(self):
        self.sources(source())
        english = component_source()
        english["i18n"]["en"]["disableEffect"] = "English effect"
        partial = copy.deepcopy(english)
        partial["i18n"]["zh-CN"] = {"description": "中文说明"}
        self.components(("com/vendor/Partial.yaml", partial), ("com/vendor/English.yaml", english))
        build(self.root)
        english_row, partial_row = self.v2_components()
        self.assertEqual(english_row["i18n"], english["i18n"])
        self.assertEqual(partial_row["i18n"], partial["i18n"])

    def test_legacy_component_text_falls_back_field_by_field_and_preserves_empty_text(self):
        component = component_source()
        component["i18n"]["en"]["disableEffect"] = "English effect"
        component["i18n"]["zh-CN"] = {"description": "中文说明"}
        partial = render_component("example.Service", component, "zh-CN", [])
        self.assertEqual(partial["description"], "中文说明")
        self.assertEqual(partial["disableEffect"], "English effect")
        component["i18n"]["zh-CN"] = {"disableEffect": ""}
        blank = render_component("example.Service", component, "zh-CN", [])
        self.assertEqual(blank["description"], "English description")
        self.assertEqual(blank["disableEffect"], "")

    def test_v2_component_sdk_uses_longest_rendered_literal_keyword_and_lowest_id_on_ties(self):
        broad = source(1, "Broad", ["SDK"])
        specific = source(9, "Specific", [])
        specific.update(upstream={"exodus": 99})
        specific["i18n"] = {"en": {}, "zh-CN": {"name": "本地 SDK"}}
        excluded = source(2, "Excluded", ["org.vendor.sdk.Child"])
        excluded["excludeKeywords"] = ["org.vendor.sdk.Child"]
        regex = source(3, "Regex", [r"org\..*"])
        regex["useRegexSearch"] = True
        tied = source(30, "Tied", ["Vendor.SDK.", "org.vendor.sdk.NotMatched"])
        self.sources(broad, specific, excluded, regex, tied)
        self.snapshot(tracker(99, "Published upstream SDK", "VENDOR.SDK."))
        components = self.components(("org/vendor/sdk/Child.yaml", component_source()))
        rendered = build(self.root)
        self.assertEqual(self.v2_components()[0]["sdkRuleId"], 9)
        legacy = render_components(components, rendered)
        self.assertEqual(legacy["en"]["org/vendor/sdk/Child.json"]["sdkName"], "Published upstream SDK")
        self.assertEqual(legacy["zh-CN"]["org/vendor/sdk/Child.json"]["sdkName"], "本地 SDK")

    def test_build_check_detects_missing_changed_and_extra_output_files_without_writing(self):
        self.sources(source())
        build(self.root)
        namespace = self.out / "v2"
        edits = (("components.jsonl", None), ("rules.json", b"{}\n"), ("extra.json", b"{}\n"))
        for name, data in edits:
            with self.subTest(name=name):
                path = namespace / name
                if data is None:
                    path.unlink()
                else:
                    path.write_bytes(data)
                before = {p: p.read_bytes() for p in self.out.rglob("*") if p.is_file()}
                with self.assertRaisesRegex(RuleError, path.relative_to(self.out).as_posix()):
                    build(self.root, check=True)
                self.assertEqual({p: p.read_bytes() for p in self.out.rglob("*") if p.is_file()}, before)
                build(self.root)
                build(self.root, check=True)

    def test_build_removes_retired_top_level_outputs(self):
        self.sources(source())
        for group in ("rules", "components", "component-details", "icon"):
            write_text(self.out / group / "old.json", "{}\n")
        write_text(self.out / "notes.txt", "Build notes\n")
        with self.assertRaisesRegex(RuleError, "component-details"):
            build(self.root, check=True)
        build(self.root)
        self.assertEqual({p.name for p in self.out.iterdir()}, {"legacy", "v2", "release", "notes.txt"})
        build(self.root, check=True)

    def test_build_copies_frozen_files_and_rejects_mismatched_checksums_or_paths(self):
        self.sources(source())
        relative = "components/en/example/Service.json"
        frozen = self.root / "legacy" / relative
        frozen.parent.mkdir(parents=True)
        payload = b'{\r\n    "description": "Frozen"\r\n}'
        frozen.write_bytes(payload)
        manifest = self.root / "legacy/components.sha256"
        checksum = f"{hashlib.sha256(payload).hexdigest()}  {relative}\n"
        manifest.write_text(checksum, encoding="utf-8")
        self.components(("example/Service.yaml", component_source()))
        build(self.root)
        self.assertEqual((self.out / "legacy" / relative).read_bytes(), payload)
        self.assertEqual(self.v2_components()[0]["i18n"]["en"]["description"], "English description")
        for changed in (b"{}", None):
            with self.subTest(changed=changed):
                if changed is None:
                    frozen.unlink()
                else:
                    frozen.write_bytes(changed)
                with self.assertRaisesRegex(RuleError, "components.sha256"):
                    build(self.root)
                self.assertEqual((self.out / "legacy" / relative).read_bytes(), payload)
        frozen.write_bytes(payload)
        manifest.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(RuleError, "components.sha256"):
            check_determinism(self.root)

    def test_catalog_manifest_lists_uncompressed_members_with_metadata(self):
        self.sources(source())
        write_text(self.root / "data/icons/nested/sample.svg", "<svg/>")
        write_text(self.root / "data/icons/first.svg", "<svg/>")
        build(self.root)
        v2 = self.out / "v2"
        manifest = v2 / "catalog.json"
        expected_files = ["components.jsonl", "icon/first.svg", "icon/nested/sample.svg", "rules.json"]
        self.assertEqual(json.loads(manifest.read_bytes()), {
            "format": 2, "sequence": 0, "sourceCommit": "", "publishedAt": "1970-01-01T00:00:00Z",
            "files": [{"path": name, "sha256": hashlib.sha256((v2 / name).read_bytes()).hexdigest(),
                       "size": (v2 / name).stat().st_size} for name in expected_files],
        })
        self.assertEqual(manifest.read_bytes(), (self.out / "release/v2-catalog.json").read_bytes())
        check_v2(v2)
        write_text(v2 / "icon/extra.svg", "<svg/>")
        with self.assertRaisesRegex(RuleError, "file paths, hashes or sizes"):
            check_v2(v2)

    def test_catalog_build_cli_records_explicit_metadata_and_rejects_invalid_options(self):
        self.sources(source())
        command = [sys.executable, str(ROOT / "scripts/rules/build.py"), "--root", str(self.root)]
        options = ["--sequence", "12", "--source-commit", "deadbeef", "--published-at", "2026-11-25T12:00:00Z"]
        subprocess.run(command + options, check=True, capture_output=True, cwd=ROOT)
        manifest_path = self.out / "v2/catalog.json"
        manifest = manifest_path.read_bytes()
        metadata = json.loads(manifest)
        self.assertEqual((metadata["sequence"], metadata["sourceCommit"], metadata["publishedAt"]),
                         (12, "deadbeef", "2026-11-25T12:00:00Z"))
        subprocess.run(command + options + ["--check"], check=True, capture_output=True, cwd=ROOT)
        preview = subprocess.run(command + ["--manifest-only"], check=True, capture_output=True, cwd=ROOT)
        self.assertEqual(json.loads(preview.stdout)["sequence"], 0)
        self.assertEqual(manifest_path.read_bytes(), manifest)
        for option, value, error in (
            ("--sequence", "-1", "non-negative integer"),
            ("--published-at", "2026-02-30T12:00:00Z", "ISO instant"),
            ("--published-at", "2026-11-25T12:00:00+00:00", "ending in Z"),
        ):
            result = subprocess.run(command + [option, value], capture_output=True, text=True, cwd=ROOT)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(error, result.stderr)
            self.assertEqual(manifest_path.read_bytes(), manifest)

    def test_release_archives_have_fixed_headers_and_unpack_to_v2_bytes(self):
        self.sources(source())
        self.components(("com/vendor/Service.yaml", component_source()))
        write_text(self.root / "data/icons/z.svg", "<svg>last</svg>")
        write_text(self.root / "data/icons/nested/中文.svg", "<svg>first</svg>")
        build(self.root)
        second = self.root / "second"
        build(self.root, output_dir=second)
        self.assertEqual({path.name for path in (self.out / "release").iterdir()}, {
            "v2-catalog.json", "v2-rules.json.gz", "v2-components.jsonl.gz", "v2-icons.zip",
        })
        for name in ("rules.json", "components.jsonl"):
            compressed = (self.out / "release" / f"v2-{name}.gz").read_bytes()
            self.assertEqual(compressed, (second / "release" / f"v2-{name}.gz").read_bytes())
            self.assertEqual(compressed[:10], b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x02\xff")
            self.assertEqual(gzip.decompress(compressed), (self.out / "v2" / name).read_bytes())
        archive_path = self.out / "release/v2-icons.zip"
        self.assertEqual(archive_path.read_bytes(), (second / "release/v2-icons.zip").read_bytes())
        with zipfile.ZipFile(archive_path) as archive:
            self.assertEqual(archive.namelist(), ["icon/nested/中文.svg", "icon/z.svg"])
            for entry in archive.infolist():
                self.assertEqual(entry.date_time, (1980, 1, 1, 0, 0, 0))
                self.assertEqual((entry.create_system, entry.external_attr >> 16), (3, 0o100644))
                self.assertEqual(entry.compress_type, zipfile.ZIP_DEFLATED)
                self.assertEqual(entry.extra, b"")
                self.assertEqual(archive.read(entry), (self.out / "v2" / entry.filename).read_bytes())

    def test_v2_validation_rejects_invalid_fields_order_and_references(self):
        rule = {"id": 1, "keywords": [], "contributors": [], "i18n": {"en": {"name": "Rule"}}}
        component = {"name": "example.Service", "recommendToBlock": False, "i18n": {"en": {"description": ""}}}
        invalid_rules = (
            ([rule, rule], "rule ids must be sorted and unique"),
            ([dict(rule, id=2), rule], "rule ids must be sorted and unique"),
            ([dict(rule, icon="icon/missing.svg")], "references missing icon"),
            ([dict(rule, website=None)], "website: expected str"),
            ([dict(rule, i18n={"en": {}})], "missing fields: name"),
        )
        invalid_components = (
            ([component, dict(component, name="example.Before")], "component names must be sorted"),
            ([{"name": component["name"], "sdkRuleId": 2, **component}], "unknown sdkRuleId"),
            ([dict(component, i18n={"en": {"description": None}})], "description: expected str"),
            ([dict(component, i18n={"en": {}})], "missing fields: description"),
        )
        for rules, rows, error in (
            [(rules, [component], error) for rules, error in invalid_rules]
            + [([rule], rows, error) for rows, error in invalid_components]
        ):
            with self.subTest(error=error):
                for name, data in catalog_files(rules, rows, {}, 0, "", "1970-01-01T00:00:00Z").items():
                    path = self.out / "v2" / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                with self.assertRaisesRegex(RuleError, error):
                    check_v2(self.out / "v2")

    def test_component_paths_allow_java_identifiers_including_dollars(self):
        self.sources(source())
        for path in ("com/vendor/$Outer$1.yaml", "com/例/_name2.yaml"):
            self.components((path, component_source()))
        build(self.root)
        self.assertEqual([row["name"] for row in self.v2_components()],
                         ["com.vendor.$Outer$1", "com.例._name2"])
        for path in ("bad-name/Service.yaml", "com/9vendor/Service.yaml", "com/vendor/Service.extra.yaml",
                     "com/class/Service.yaml", "com/_/Service.yaml", "../Escape.yaml", "/absolute/Name.yaml"):
            with self.subTest(path=path), self.assertRaisesRegex(RuleError, "Java identifiers"):
                component_name(path)
        write_text(self.root / "data/components/bad-name/Service.yaml", yaml_text(component_source()))
        with self.assertRaisesRegex(RuleError, "Java identifiers"):
            build(self.root, check=True)

    def test_component_schema_rejects_unknown_fields_types_and_missing_description_before_writes(self):
        self.sources(source())
        invalid = [[], None]
        for field, value in (("name", "forbidden"), ("sdkName", "forbidden"),
                             ("addedVersion", "1"), ("removedVersion", "2"), ("extra", True),
                             ("recommendToBlock", 1), ("contributor", None), ("contributor", []),
                             ("lastUpdateTime", 123), ("lastUpdateTime", "yesterday"),
                             ("lastUpdateTime", "2023-02-30T16:45:01Z")):
            item = component_source()
            item[field] = value
            invalid.append(item)
        for i18n in (None, {}, {"en": {}}, {"en": []}, {"en": {"description": None}},
                     {"en": {"description": 1}}, {"en": {"description": "ok", "name": "forbidden"}},
                     {"en": {"description": "ok"}, "zh_CN": {}},
                     {"en": {"description": "ok"}, "zh-CN": {"contributor": "forbidden"}},
                     {"en": {"description": "ok"}, "zh-CN": {"disableEffect": False}}):
            item = component_source()
            item["i18n"] = i18n
            invalid.append(item)
        missing = component_source()
        missing.pop("recommendToBlock")
        invalid.append(missing)
        for index, item in enumerate(invalid):
            with self.subTest(case=index):
                write_text(self.root / "data/components/com/vendor/Service.yaml", yaml_text(item))
                with self.assertRaises(RuleError):
                    build(self.root)
                self.assertFalse(self.out.exists())

    def test_component_duplicate_keys_and_unquoted_timestamps_are_rejected(self):
        self.sources(source())
        path = self.root / "data/components/com/vendor/Service.yaml"
        write_text(path, "recommendToBlock: false\nrecommendToBlock: true\ni18n:\n  en:\n    description: ok\n")
        with self.assertRaisesRegex(RuleError, "duplicate YAML key"):
            build(self.root)
        write_text(path, "recommendToBlock: false\nlastUpdateTime: 2023-11-19T16:45:01.937788900Z\n"
                        "i18n:\n  en:\n    description: ok\n")
        with self.assertRaisesRegex(RuleError, "lastUpdateTime: expected str"):
            build(self.root)
        self.assertFalse(self.out.exists())

    def test_component_migration_preserves_text_reports_conflicts_and_refuses_overwrite(self):
        self.sources(source())
        en = {"name": "com.vendor.Service", "sdkName": "2131820718", "description": "English description",
              "disableEffect": "Same effect", "contributor": "Mercury", "recommendToBlock": False,
              "lastUpdateTime": "2023-11-19T16:45:01.937788900Z"}
        zh = dict(en, description="中文说明", contributor="Other", recommendToBlock=True)
        zh.pop("lastUpdateTime")
        for lang, row in (("en", en), ("zh-CN", zh)):
            write_text(self.root / f"components/{lang}/com/vendor/Service.json", component_json_text(row))
        report = migrate_components(self.root, self.root)
        self.assertEqual(report["migrated"], 1)
        self.assertEqual(report["zh-CN_only"], [])
        self.assertEqual(report["en_missing_description"], 0)
        self.assertEqual(report["dropped_sdkName"], {"en": 1, "zh-CN": 1})
        self.assertEqual([c["field"] for c in report["conflicts"]],
                         ["contributor", "recommendToBlock", "lastUpdateTime"])
        migrated = load_component_sources(self.root)[0][1]
        self.assertEqual(set(migrated), {"recommendToBlock", "contributor", "lastUpdateTime", "i18n"})
        self.assertEqual(migrated["i18n"]["zh-CN"], {"description": "中文说明"})
        self.assertEqual(migrated["lastUpdateTime"], en["lastUpdateTime"])
        raw = (self.root / "data/components/com/vendor/Service.yaml").read_text()
        self.assertIn("lastUpdateTime: '2023-11-19T16:45:01.937788900Z'", raw)
        build(self.root)
        self.assertEqual(self.v2_components(), [{
            "name": en["name"], "recommendToBlock": False, "contributor": "Mercury",
            "updatedAt": en["lastUpdateTime"], "i18n": migrated["i18n"],
        }])
        with self.assertRaisesRegex(RuleError, "refuses to overwrite"):
            migrate_components(self.root, self.root)

    def test_component_migration_omits_identical_chinese_text(self):
        row = {"name": "com.vendor.Service", "description": "Shared", "recommendToBlock": False}
        for lang in ("en", "zh-CN"):
            write_text(self.root / f"components/{lang}/com/vendor/Service.json", component_json_text(row))
        report = migrate_components(self.root, self.root)
        self.assertEqual(report["conflicts"], [])
        self.assertEqual(load_component_sources(self.root)[0][1]["i18n"], {"en": {"description": "Shared"}})

    def test_component_migration_does_not_invent_english_for_chinese_only_components(self):
        write_text(self.root / "components/zh-CN/com/vendor/Service.json", component_json_text({
            "name": "com.vendor.Service", "description": "中文说明", "recommendToBlock": False,
        }))
        with self.assertRaisesRegex(RuleError, "only in zh-CN.*com.vendor.Service"):
            migrate_components(self.root, self.root)
        self.assertFalse((self.root / "data/components").exists())

    def test_given_stale_components_when_checked_then_only_build_checks_freshness(self):
        self.sources(source())
        self.components(("com/vendor/Service.yaml", component_source()))
        build(self.root)
        path = self.out / "v2/components.jsonl"
        path.write_text("{}\n")
        for cli, flags in (("build.py", ["--check"]), ("validate.py", ["--check"]), ("validate.py", [])):
            result = subprocess.run([sys.executable, str(ROOT / "scripts/rules" / cli), "--root", str(self.root), *flags],
                                    capture_output=True, text=True, cwd=ROOT)
            if cli == "build.py":
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("v2/components.jsonl", result.stderr)
            else:
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(path.read_text(), "{}\n")

    def test_given_icons_when_built_then_output_mirrors_source_bytes(self):
        self.sources(source())
        icon = self.root / "data/icons/sample.png"
        icon.parent.mkdir(parents=True)
        icon.write_bytes(b"\x00\xff\r\n")
        output = self.out / "legacy/icon/sample.png"
        build(self.root)
        self.assertEqual(output.read_bytes(), icon.read_bytes())
        self.assertEqual((self.out / "v2/icon/sample.png").read_bytes(), icon.read_bytes())
        build(self.root, check=True)
        output.write_bytes(b"changed")
        with self.assertRaisesRegex(RuleError, "icon/sample.png"):
            build(self.root, check=True)
        icon.unlink()
        build(self.root)
        self.assertFalse(output.exists())

    def test_given_different_output_bytes_when_validated_then_determinism_check_fails(self):
        self.sources(source())

        def changing_build(root, *, output_dir):
            build(root, output_dir=output_dir)
            write_text(output_dir / "release/v2-rules.json.gz", f"{output_dir.name}\n")

        with mock.patch("validate.build", side_effect=changing_build):
            with self.assertRaisesRegex(RuleError, "builds differ: release/v2-rules.json.gz"):
                check_determinism(self.root)
        self.assertFalse(self.out.exists())

    def test_merge_upstream_first_dedup_strip_and_exclude_both_sources(self):
        self.assertEqual(
            merge_keywords("  com.up. | |com.same|com.up.|com.drop ",
                             ["com.local", "com.same", "com.drop"], ["com.drop"]),
            ["com.up.", "com.same", "com.local"],
        )

    def test_code_merge_keeps_local_spelling_and_normalizes_exclusions(self):
        self.assertEqual(
            merge_keywords("com.facebook.ads|com.new.|com.drop",
                           ["com.facebook.ads.", "com.facebook.ads", "com.local."], ["com.drop."]),
            ["com.facebook.ads.", "com.new.", "com.local."],
        )
        self.assertEqual(merge_keywords("com.same.|com.same", ["com.same"]), ["com.same"])

    def test_upstream_code_subsumed_by_local_keyword_is_not_added(self):
        narrow = [f"com.google.android.gms.ads.{name}" for name in
                  ("AdView", "AdActivity", "AdRequest", "mediation", "doubleclick")]
        self.assertEqual(
            merge_keywords("|".join(narrow + ["org.new.Sdk", "com.google.android.gms.ads"]),
                           ["com.google.android.gms.ads.", "legacy.Component"]),
            ["com.google.android.gms.ads.", "org.new.Sdk", "legacy.Component"],
        )
        # Existing local keywords are preserved even if an upstream keyword is broader.
        self.assertEqual(merge_keywords("com.sdk", ["com.sdk.Child"]), ["com.sdk", "com.sdk.Child"])

    def test_code_subsumption_matches_case_insensitive_nonprefix_substrings(self):
        self.assertEqual(merge_keywords("org.vendor.SDK.PurchaseService|org.vendor.NewSdk", ["sdk.purchase."]),
                         ["sdk.purchase.", "org.vendor.NewSdk"])

    def test_excluded_local_keyword_does_not_subsume_an_upstream_entry(self):
        self.assertEqual(merge_keywords("com.vendor.sdk.RemoteService", ["com.vendor.sdk."], ["com.vendor.sdk"]),
                         ["com.vendor.sdk.RemoteService"])

    def test_network_merge_deduplicates_only_exact_strings(self):
        self.assertEqual(merge_signatures("ads.example.|ads.example|ads.example", ["ads.example", "local"]),
                         ["ads.example.", "ads.example", "local"])
        self.assertEqual(merge_signatures(r"^adserver\.vendor\.com$", [r"adserver\.vendor\.com"]),
                         [r"^adserver\.vendor\.com$", r"adserver\.vendor\.com"])

    def test_missing_keys_and_source_metadata_never_leak(self):
        rule = source(keywords=[])
        rule["upstream"] = {"exodus": 3}
        t = tracker(3, "Remote", "")
        # Even an untrimmed response cannot inject a safety assertion.
        t.update(safeToBlock=True, sideEffect="unsafe inference", company="not supplied by Exodus")
        output = render_rule(rule, "en", {3: t})
        self.assertEqual(set(output), {"id", "name", "description"})
        self.assertFalse(any(v is None for v in output.values()))

    def test_text_fallback_preserves_explicit_empty_strings_and_false(self):
        rule = source()
        rule.update(upstream={"exodus": 3}, safeToBlock=False, useRegexSearch=False)
        rule["i18n"]["en"].update(description="", sideEffect="")
        rule["i18n"]["zh-CN"] = {"name": "本地"}
        output = render_rule(rule, "zh-CN", {3: tracker(3, "Upstream", "com.remote")})
        self.assertEqual(output["name"], "本地")
        self.assertEqual(output["description"], "")
        self.assertEqual(output["sideEffect"], "")
        self.assertIs(output["safeToBlock"], False)
        self.assertIs(output["useRegexSearch"], False)

    def test_upstream_english_text_falls_back_into_chinese(self):
        rule = source()
        rule["upstream"] = {"exodus": 3}
        rule["i18n"] = {"en": {}}
        trackers = {3: tracker(3, "Remote English", "com.remote")}
        self.assertEqual(render_rule(rule, "zh-CN", trackers)["name"], "Remote English")
        self.assertEqual(check_rules([(Path("r.yaml"), rule)], trackers), [])

    def test_language_neutral_metadata_is_identical_in_both_outputs(self):
        rule = source()
        rule.update(website="local", contributors=["English"],
                    networkSignature=["local\\.test"])
        rule["i18n"]["en"]["company"] = "English company"
        rule["i18n"]["zh-CN"] = {"name": "本地", "company": "本地公司"}
        en, zh = (render_rule(rule, lang, {}) for lang in ("en", "zh-CN"))
        for field in ("contributors", "website", "networkSignature"):
            self.assertEqual(en[field], zh[field])
        self.assertEqual(zh["name"], "本地")
        self.assertEqual(en["company"], "English company")
        self.assertEqual(zh["company"], "本地公司")

    def test_company_is_localized_and_falls_back_to_english(self):
        rule = source()
        rule["i18n"]["en"]["company"] = "Tencent Holdings Limited"
        rule["i18n"]["zh-CN"] = {"name": "腾讯广告"}
        self.assertEqual(render_rule(rule, "zh-CN", {})["company"], "Tencent Holdings Limited")
        rule["i18n"]["zh-CN"]["company"] = "腾讯"
        self.assertEqual(render_rule(rule, "zh-CN", {})["company"], "腾讯")
        self.assertEqual(render_rule(rule, "en", {})["company"], "Tencent Holdings Limited")
        self.sources(rule)

    def test_top_level_company_is_rejected(self):
        rule = source()
        rule["company"] = "obsolete source layout"
        with self.assertRaisesRegex(RuleError, "unknown source keys"):
            self.sources(rule)

    def test_non_text_translation_fields_are_rejected(self):
        rule = source()
        rule["i18n"]["zh-CN"] = {"website": "https://unshared.test/"}
        with self.assertRaisesRegex(RuleError, "text fields"):
            self.sources(rule)

    def test_duplicate_ids_rejected_before_build_writes(self):
        with self.assertRaisesRegex(RuleError, "duplicate rule id"):
            self.sources(source(), source())
        self.assertFalse(self.out.exists())

    def test_duplicate_yaml_key_rejected(self):
        write_text(self.root / "data/rules/bad.yaml", "id: 1\nid: 2\n")
        with self.assertRaisesRegex(RuleError, "duplicate YAML key"):
            load_sources(self.root)

    def test_unknown_fields_nulls_and_boolean_id_rejected(self):
        for field, value in (("id", True), ("safeToBlock", None), ("keywords", [None]),
                             ("accidentalPublishedKey", "x")):
            with self.subTest(field=field):
                rule = source()
                rule[field] = value
                write_text(self.root / "data/rules/rule-0.yaml", yaml_text(rule))
                with self.assertRaises(RuleError):
                    load_sources(self.root)

    def test_missing_english_name_rejected(self):
        rule = source()
        rule["i18n"]["en"].pop("name")
        self.sources(rule)
        with self.assertRaisesRegex(RuleError, "English name"):
            prepared(self.root)

    def test_regex_validation_covers_code_and_network(self):
        rule = source(keywords=["["])
        rule["useRegexSearch"] = True
        self.sources(rule)
        with self.assertRaisesRegex(RuleError, "invalid en regex"):
            prepared(self.root)
        rule["keywords"] = ["valid"]
        rule["networkSignature"] = ["("]
        self.sources(rule)
        with self.assertRaisesRegex(RuleError, "invalid en regex"):
            prepared(self.root)
        rule["useRegexSearch"] = False
        self.sources(rule)
        prepared(self.root)

    def test_empty_keywords_warn_and_remain_published(self):
        self.sources(source(keywords=[]))
        _, _, rendered, warnings = prepared(self.root)
        self.assertEqual(len(warnings), 1)
        self.assertIn("empty searchKeyword", warnings[0])
        self.assertEqual(len(rendered["en"]), 1)
        self.assertNotIn("searchKeyword", rendered["en"][0])

    def test_build_check_detects_manual_edits_without_repairing_them(self):
        self.sources(source())
        build(self.root)
        path = self.out / "legacy/rules/en/general.json"
        edited = path.read_text() + " "
        path.write_text(edited)
        with self.assertRaisesRegex(RuleError, "out of date"):
            build(self.root, check=True)
        self.assertEqual(path.read_text(), edited)

    def test_build_is_byte_stable_sorted_unicode_and_newline_terminated(self):
        self.sources(source(20), source(2, "中文"))
        build(self.root)
        path = self.out / "legacy/rules/en/general.json"
        content = path.read_bytes()
        self.assertIn("中文".encode(), content)
        self.assertTrue(content.endswith(b"\n"))
        self.assertEqual([r["id"] for r in json.loads(content)], [2, 20])
        build(self.root)
        build(self.root, check=True)
        self.assertEqual(path.read_bytes(), content)

    def test_trim_snapshot_rejects_bad_data_and_strips_extra_api_fields(self):
        a, b = tracker(8, "A", "com.a"), tracker(3, "B", "com.b")
        a.update(creation_date="unused", report_count=12345, safeToBlock=True)
        a["categories"] = ["Location", "Analytics", "Location"]
        trimmed = trim_trackers({"trackers": {"8": a, "3": b}})
        self.assertEqual([t["id"] for t in trimmed], [3, 8])
        self.assertEqual(set(trimmed[1]), set(rulelib.TRACKER_FIELDS))
        self.assertEqual(trimmed[1]["categories"], ["Analytics", "Location"])
        with self.assertRaisesRegex(RuleError, "key/id mismatch"):
            trim_trackers({"trackers": {"9": b}})
        with self.assertRaisesRegex(RuleError, "expected a string"):
            trim_trackers({"trackers": {"3": dict(b, code_signature=None)}})

    def test_fetch_uses_real_endpoint_and_trims_response(self):
        api = {"trackers": {"3": dict(tracker(3, "A", "com.a"), extraneous=123)}}
        response = io.BytesIO(json.dumps(api).encode())
        with mock.patch("urllib.request.urlopen", return_value=response) as urlopen:
            result = fetch_trackers()
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, EXODUS_URL)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 45)
        self.assertNotIn("extraneous", result[0])

    def test_optional_api_token_is_sent_only_as_an_authentication_header(self):
        response = io.BytesIO(json.dumps({"trackers": {"3": tracker(3, "A", "com.a")}}).encode())
        with mock.patch.dict("os.environ", {"EXODUS_API_TOKEN": "test-token"}):
            with mock.patch("urllib.request.urlopen", return_value=response) as urlopen:
                fetch_trackers()
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Token test-token")
        self.assertNotIn("test-token", request.full_url)

    def test_failed_fetch_preserves_snapshot_and_sources(self):
        self.sources(source())
        self.snapshot(tracker(3, "Old", "com.old"))
        old_files = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("DNS unavailable")):
            with self.assertRaises(urllib.error.URLError):
                sync(self.root)
        self.assertEqual(old_files, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_empty_api_does_not_replace_existing_snapshot(self):
        with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(b'{"trackers": {}}')):
            with self.assertRaisesRegex(RuleError, "no trackers"):
                fetch_trackers()

    def test_prefix_matching_has_package_boundaries_and_no_regex_guessing(self):
        self.assertTrue(prefix_overlap("com.vendor.", "com.vendor.sdk"))
        self.assertTrue(prefix_overlap("com.vendor.", "com.vendor"))
        self.assertFalse(prefix_overlap("com.vendor", "com.vendorish"))
        self.assertFalse(prefix_overlap(".vendor.", "com.vendor"))
        self.assertFalse(prefix_overlap("com.v[0-9]", "com.v1.sdk"))

    def test_linking_requires_reciprocal_unique_exact_evidence(self):
        sources = self.sources(source(1, "Unique", ["com.unique."]),
                               source(2, "Shared", ["com.shared."]),
                               source(3, "Shared nested", ["com.shared.nested"]),
                               source(4, "Prefix only", ["com.prefix"]),
                               source(5, "Multiple", ["com.multi"]),
                               source(6, "Unmatched", ["com.unmatched"]))
        trackers = {t["id"]: t for t in (
            tracker(20, "Unique upstream", "com.unique|com.unique.more"),
            tracker(21, "Shared", "com.shared"),
            tracker(22, "Prefix", "com.prefix.deep"),
            tracker(23, "Multi1", "com.multi"),
            tracker(24, "Multi2", "com.multi.extra"),
        )}
        certain, ambiguous, unmatched = link_candidates(sources, trackers)
        self.assertEqual([(r["id"], tid) for _, r, tid in certain], [(1, 20)])
        self.assertEqual({r["id"] for _, r, _ in ambiguous}, {2, 3, 4, 5})
        self.assertEqual([r["id"] for _, r in unmatched], [6])

    def test_link_dry_run_is_read_only_and_real_link_changes_only_signatures(self):
        self.sources(source(10, "Local", ["com.unique."]))
        self.snapshot(tracker(3, "Remote rename", "com.unique|com.new", "new\\.test"))
        source_path = self.root / "data/rules/rule-0.yaml"
        before_source = source_path.read_bytes()
        before = render_all(load_sources(self.root), load_snapshot(self.root))
        link(self.root, dry_run=True)
        self.assertEqual(source_path.read_bytes(), before_source)
        self.assertFalse((self.root / "upstream/exodus/link_candidates.md").exists())
        certain, _, report = link(self.root)
        self.assertEqual(len(certain), 1)
        self.assertIn("1 code keywords added to 1 rules", report)
        after = render_all(load_sources(self.root), load_snapshot(self.root))
        for lang in before:
            for key in before[lang][0]:
                if key not in ("searchKeyword", "networkSignature"):
                    self.assertEqual(before[lang][0][key], after[lang][0][key])
        self.assertEqual(after["en"][0]["id"], 10)
        self.assertEqual(after["en"][0]["searchKeyword"], ["com.unique.", "com.new"])
        self.assertEqual(len(link(self.root)[0]), 0)

    def test_sync_allocates_app_ids_adds_rules_and_is_idempotent(self):
        rule = source(99)
        rule["upstream"] = {"exodus": 2}
        self.sources(rule)
        self.snapshot(tracker(2, "Old", "com.old"), tracker(900, "Brand new", "com.new"),
                      tracker(901, "No code", " | "))
        added, _ = sync(self.root, from_snapshot=True)
        self.assertEqual([r["id"] for r in added], [100])
        self.assertNotIn("safeToBlock", added[0])
        self.assertNotIn("company", added[0])
        self.assertNotIn("company", added[0]["i18n"]["en"])
        self.assertEqual(added[0]["upstream"], {"exodus": 900})
        before = {p: p.read_bytes() for p in (self.root / "data/rules").glob("*.yaml")}
        self.assertEqual(len(sync(self.root, from_snapshot=True)[0]), 0)
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertEqual(len(render_all(load_sources(self.root), load_snapshot(self.root))["en"]), 2)
        for lang in ("en", "zh-CN"):
            self.assertNotIn("company", next(r for r in render_all(load_sources(self.root), load_snapshot(self.root))[lang]
                                            if r["id"] == 100))

    def test_upstream_removal_retains_local_rule_id_and_available_data(self):
        rule = source(99)
        rule["upstream"] = {"exodus": 7}
        sources = self.sources(rule)
        warnings = check_rules(sources, {})
        self.assertIn("missing from snapshot", warnings[0])
        output = render_all(sources, {})["en"][0]
        self.assertEqual(output["id"], 99)
        self.assertEqual(output["searchKeyword"], ["com.local."])

    def test_sync_does_not_import_prefix_overlap_even_without_an_exact_match(self):
        self.sources(source(10, "Local SDK", ["com.vendor.sdk."]))
        self.snapshot(tracker(900, "Upstream SDK", "com.vendor.sdk.nested"))
        before = list((self.root / "data/rules").glob("*.yaml"))
        added, report = sync(self.root, from_snapshot=True)
        self.assertEqual(added, [])
        self.assertEqual(before, list((self.root / "data/rules").glob("*.yaml")))
        self.assertIn("## Needs manual link", report)
        self.assertIn("Exodus 900 Upstream SDK", report)
        self.assertIn("app 10 Local SDK", report)
        self.assertIn("com.vendor.sdk.", report)
        self.assertIn("No rule created", report)

    def _assert_fragment_overlap_needs_manual_link(self, rule, remote):
        sources = self.sources(rule)
        self.snapshot(remote)
        trackers = load_snapshot(self.root)
        # Weak substring evidence must suppress imports without creating a link.
        certain, ambiguous, _ = link_candidates(sources, trackers)
        self.assertEqual(certain, [])
        self.assertEqual(ambiguous, [])
        before = {path: path.read_bytes() for path, _ in sources}
        added, report = sync(self.root, from_snapshot=True)
        self.assertEqual(added, [])
        self.assertEqual(before, {path: path.read_bytes() for path, _ in load_sources(self.root)})
        manual = report.split("## Needs manual link\n\n", 1)[1].split("\n##", 1)[0]
        self.assertIn(f"Exodus {remote['id']} {remote['name']}", manual)
        self.assertIn(f"app {rule['id']} {rule['i18n']['en']['name']}", manual)
        self.assertIn("No rule created", manual)

    def test_sync_guard_suppresses_amazon_leading_dot_fragment(self):
        self._assert_fragment_overlap_needs_manual_link(
            source(23, "Amazon Ads", [".amazon.device.ads."]),
            tracker(92, "Amazon Advertisement", "com.amazon.device.ads"),
        )

    def test_sync_guard_suppresses_supersonic_leading_dot_fragment(self):
        self._assert_fragment_overlap_needs_manual_link(
            source(2, "SuperSonic", [".supersonicads."]),
            tracker(110, "Supersonic Ads", "com.supersonic.adapters.supersonicads|com.supersonicads.sdk"),
        )

    def test_sync_guard_suppresses_jpush_leading_dot_fragment(self):
        self._assert_fragment_overlap_needs_manual_link(
            source(15, "Aurora Push", [".jpush.", ".jiguang."]),
            tracker(343, "JiGuang Aurora Mobile JPush", "cn.jpush.android"),
        )

    def test_sync_guard_checks_reverse_substrings_case_insensitively(self):
        self._assert_fragment_overlap_needs_manual_link(
            source(10, "Nested SDK", [".VENDOR.SDK.Child."]),
            tracker(900, "Upstream SDK", "vendor.sdk"),
        )

    def test_sync_prefix_check_includes_upstream_only_sources(self):
        rule = source(10, "Pending SDK", [])
        rule["upstream"] = {"exodus": 800}
        self.sources(rule)
        self.snapshot(tracker(800, "Pending SDK", "com.vendor.sdk"),
                      tracker(900, "Overlapping SDK", "com.vendor.sdk.extra"))
        added, report = sync(self.root, from_snapshot=True)
        self.assertEqual(added, [])
        self.assertIn("Exodus 900 Overlapping SDK", report)

    def test_newly_staged_source_prevents_a_second_overlapping_import(self):
        self.sources(source())
        self.snapshot(tracker(800, "New SDK", "com.brandnew"),
                      tracker(900, "Nested SDK", "com.brandnew.extra"))
        added, report = sync(self.root, from_snapshot=True)
        self.assertEqual([r["upstream"]["exodus"] for r in added], [800])
        self.assertIn("Exodus 900 Nested SDK", report)
        self.assertEqual(sync(self.root, from_snapshot=True)[0], [])

    def test_ignored_tracker_is_never_imported_even_without_local_overlap(self):
        self.sources(source())
        self.snapshot(tracker(900, "Ignored SDK", "com.unrelated"))
        self.ignore({"id": 900, "reason": "Broad unsafe namespace"})
        added, report = sync(self.root, from_snapshot=True)
        self.assertEqual(added, [])
        self.assertIn("## Ignored trackers", report)
        self.assertIn("Exodus 900: Broad unsafe namespace", report)
        self.assertEqual(sync(self.root, from_snapshot=True)[0], [])

    def test_ignored_tracker_is_also_excluded_from_automatic_linking(self):
        self.sources(source(10, "Local", ["com.same."]))
        self.snapshot(tracker(900, "Ignored", "com.same"))
        self.ignore({"id": 900, "reason": "Requires review"})
        certain, ambiguous, _ = link(self.root)
        self.assertEqual(certain, [])
        self.assertEqual(ambiguous, [])
        self.assertNotIn("upstream", load_sources(self.root)[0][1])

    def test_ignore_configuration_errors_fail_validation_before_writes(self):
        self.sources(source())
        for entries in ([{"id": True, "reason": "invalid id"}], [{"id": 900, "reason": ""}],
                        [{"id": 900, "reason": "A"}, {"id": 900, "reason": "B"}]):
            write_text(self.root / "upstream/exodus/ignore.yaml", yaml_text(entries))
            with self.assertRaises(RuleError):
                prepared(self.root)
        self.ignore({"id": 900, "reason": "Intentional suppression"})
        self.assertEqual(load_ignored(self.root), {900: "Intentional suppression"})

    def test_from_snapshot_does_not_reformat_or_replace_supplied_data(self):
        self.sources(source())
        self.snapshot(tracker(3, "New", "com.new"))
        path = self.root / "upstream/exodus/trackers.json"
        original = json.dumps(json.loads(path.read_text()), separators=(",", ":")).encode()
        path.write_bytes(original)
        sync(self.root, from_snapshot=True)
        self.assertEqual(path.read_bytes(), original)

    def test_known_duplicate_imports_are_linked_suppressed_or_ignored(self):
        self.sources(source(101, "AppLovin", ["com.applovin"]),
                     source(168, "Gigya", ["com.gigya."]),
                     source(4, "Sensors Data", ["com.sensorsdata.analytics."]),
                     source(1, "Pangolin", ["com.bytedance.sdk.openadsdk."]))
        self.snapshot(tracker(72, "AppLovin", "com.applovin"),
                      tracker(148, "Gigya", "com.gigya."),
                      tracker(248, "Sensors Analytics", "com.sensorsdata.analytics.android.sdk"),
                      tracker(363, "Pangle", "com.bytedance|com.pgl|com.pangle.global"))
        self.ignore({"id": 363, "reason": "Whole ByteDance namespace includes TikTok internals"})
        certain, ambiguous, _ = link(self.root)
        self.assertEqual({(r["id"], tid) for _, r, tid in certain}, {(101, 72), (168, 148)})
        self.assertEqual([r["id"] for _, r, _ in ambiguous], [4])
        added, report = sync(self.root, from_snapshot=True)
        self.assertEqual(added, [])
        self.assertIn("Exodus 248 Sensors Analytics", report)
        self.assertIn("Exodus 363: Whole ByteDance", report)
        self.assertEqual(len(load_sources(self.root)), 4)
        self.assertEqual(sync(self.root, from_snapshot=True)[0], [])

    def test_migration_removes_strays_preserves_honor_and_unifies_metadata(self):
        en = []
        for rule_id, name, keyword in (
            (35, "Vivo Advertising Alliance", "com.vivo"),
            (36, "Oppo Push Service", "com.oppo.push"),
            (37, "Oppo Advertising Alliance", "com.oppo.ads"),
            (35, "Teemo", "com.teemo"), (36, "FidZup", "com.fidzup"),
            (37, "Audience Studio (Krux)", "com.krux"),
            (465, "Sohu In-App Ads", "com.sohu"),
            (465, "Honor Ad Alliance", "com.honor"),
            (168, "Gigya", "com.gigya."), (101, "AppLovin", "com.applovin"),
            (467, "SAP CDC (Gigya)", "com.gigya."),
            (371, "AutoNavi", "com.amap"), (39, "Google DoubleClick", None),
        ):
            row = {"id": rule_id, "name": name, "company": "English", "description": name, "contributors": ["A"]}
            if keyword:
                row["searchKeyword"] = [keyword]
            en.append(row)
        named = {row["name"]: row for row in en}
        named["AutoNavi"]["networkSignature"] = ["a", "a", "b"]
        named["SAP CDC (Gigya)"].update(networkSignature=["cdn.gigya"], website="https://gigya.test/")
        named["AppLovin"].update(networkSignature=["ads.test"], website="https://ads.test/")
        en[0]["safeToBlock"] = False
        en[0]["useRegexSearch"] = False
        zh = copy.deepcopy(en)
        zh[0]["company"], zh[0]["contributors"] = "中文公司", ["A", "中文", "A"]
        zh_named = {row["name"]: row for row in zh}
        zh_named["Honor Ad Alliance"]["id"], zh_named["Honor Ad Alliance"]["name"] = 466, "荣耀广告联盟"
        zh_named["AppLovin"].pop("networkSignature")
        zh_named["AppLovin"].pop("website")
        zh_named["SAP CDC (Gigya)"].update(name="AppLovin copy", searchKeyword=["com.applovin"],
                                               networkSignature=["ads.test", "additional.test"], website="https://ads.test/")
        for lang, rows in (("en", en), ("zh-CN", zh)):
            write_text(self.root / f"rules/{lang}/general.json", json_text(rows))
        manifest = migrate(self.root, self.root, legacy_ref="fixture")
        self.assertEqual([r["new_id"] for r in manifest["id_reassignments"]], [468, 469, 470, 466])
        self.assertEqual(manifest["keyword_merges"], [])
        self.assertEqual(len(manifest["removed_rules"]), 2)
        self.assertEqual(manifest["id_reassignments"][-1]["old_zh-CN_id"], 466)
        rendered = build(self.root)
        for lang, rows in (("en", en), ("zh-CN", zh)):
            indexed = {r["id"]: r for r in rendered[lang]}
            self.assertNotIn(467, indexed)
            self.assertNotIn(471, indexed)
            self.assertIn(466, indexed)
            self.assertEqual(indexed[168]["networkSignature"], ["cdn.gigya"])
            self.assertEqual(indexed[168]["website"], "https://gigya.test/")
            self.assertEqual(indexed[101]["networkSignature"], ["ads.test", "additional.test"])
            self.assertEqual(indexed[101]["website"], "https://ads.test/")
            self.assertEqual(indexed[35]["contributors"], ["A", "中文"])
            self.assertEqual(indexed[35]["company"], "中文公司" if lang == "zh-CN" else "English")
            changes = compare(rows, rendered[lang], lang, manifest)
            self.assertTrue(any("REMOVED" in line for line in changes))
            self.assertFalse(any(": safeToBlock:" in line for line in changes))
        with self.assertRaisesRegex(RuleError, "refuses to overwrite"):
            migrate(self.root, self.root, legacy_ref="fixture")

    def test_compare_detects_new_removed_and_absent_versus_empty(self):
        old = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
        new = [{"id": 1, "name": "A", "searchKeyword": []}, {"id": 3, "name": "C"}]
        result = compare(old, new, "en", {"id_reassignments": []})
        self.assertIn("<absent> -> []", result[0])
        self.assertIn("REMOVED", result[1])
        self.assertIn("ADDED", result[2])

    def test_compare_groups_translated_company_changes_by_category(self):
        old = [{"id": i, "name": f"Rule {i}", "company": "中文", "contributors": ["A"]}
               for i in range(1, 41)]
        current = [dict(row, company="English", contributors=["A", "B"]) for row in old]
        changes = differences(old, current, "zh-CN", {"id_reassignments": []})
        self.assertEqual(summarize(changes), ["zh-CN: localized company: 40",
                                             "zh-CN: ordered contributor unions: 40"])

    def test_unpairable_migration_fails_before_writing(self):
        with self.assertRaisesRegex(RuleError, "cannot safely pair"):
            pair_languages([{"id": 1, "name": "A", "searchKeyword": ["com.a"]}], [])

    def test_all_clis_complete_offline_pipeline_with_explicit_api_fixture(self):
        legacy_root = self.root / "published"
        row = {"id": 7, "name": "Alpha", "searchKeyword": ["com.alpha."], "description": "Local"}
        for lang in ("en", "zh-CN"):
            write_text(legacy_root / f"rules/{lang}/general.json", json_text([row]))
        write_text(legacy_root / "components/en/com/alpha/Service.json", component_json_text({
            "name": "com.alpha.Service", "description": "English description", "recommendToBlock": False,
        }))
        fixture = self.root / "api-fixture.json"
        write_text(fixture, json_text({"trackers": {
            "80": dict(tracker(80, "Alpha upstream", "com.alpha|com.extra"), extraneous=123),
            "90": tracker(90, "Beta", "com.beta"),
        }}))
        commands = [
            ["migrate_from_json.py", "--legacy-root", str(legacy_root), "--output-root", str(self.root), "--legacy-ref", "fixture"],
            ["migrate_components.py", "--legacy-root", str(legacy_root), "--output-root", str(self.root)],
            ["sync_exodus.py", "--root", str(self.root), "--snapshot-only", "--input", str(fixture)],
            ["link_exodus.py", "--root", str(self.root), "--summary-file", str(self.root / "link-summary.md")],
            ["sync_exodus.py", "--root", str(self.root), "--from-snapshot"],
            ["build.py", "--root", str(self.root)],
            ["build.py", "--root", str(self.root), "--check"],
            ["validate.py", "--root", str(self.root), "--check"],
            ["compare_legacy.py", "--root", str(self.root), "--legacy-root", str(legacy_root)],
        ]
        for args in commands:
            result = subprocess.run([sys.executable, str(ROOT / "scripts/rules" / args[0]), *args[1:]],
                                    capture_output=True, text=True, cwd=ROOT)
            self.assertEqual(result.returncode, 0, f"{args[0]}: {result.stdout}\n{result.stderr}")
        self.assertEqual(len(load_sources(self.root)), 2)
        self.assertEqual(self.v2_components(), [{
            "name": "com.alpha.Service", "sdkRuleId": 7, "recommendToBlock": False,
            "i18n": {"en": {"description": "English description"}},
        }])
        self.assertEqual(set(load_snapshot(self.root)[80]), set(rulelib.TRACKER_FIELDS))
        self.assertIn("Beta", (self.root / ".local/exodus-sync-summary.md").read_text())
        self.assertIn("1 code keywords", (self.root / "link-summary.md").read_text())
        # The build freshness check reports a manual JSON edit without repairing it.
        generated = self.out / "legacy/rules/en/general.json"
        generated.write_text("[]\n")
        result = subprocess.run([sys.executable, str(ROOT / "scripts/rules/build.py"), "--root", str(self.root), "--check"],
                                capture_output=True, text=True, cwd=ROOT)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("out of date", result.stderr)
        self.assertEqual(generated.read_text(), "[]\n")


if __name__ == "__main__":
    unittest.main()
