import hashlib
import io
import json
import os
import runpy
import subprocess
import sys
import tarfile
import tempfile
import unittest

from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import monitoring  # noqa: E402
from agent_monitor import dashboard, models, parsers, tui, updater, views  # noqa: E402


class ModelModuleTests(unittest.TestCase):
    def test_compatibility_module_reexports_domain_types(self):
        self.assertIs(monitoring.Usage, models.Usage)
        self.assertIs(monitoring.PromptTurn, models.PromptTurn)
        self.assertIs(monitoring.OverallReport, models.OverallReport)

    def test_usage_tracks_raw_context_and_weighted_consumption(self):
        usage = models.Usage()
        usage.add("model-a", 10, 5, 2, 30)
        self.assertEqual(usage.total, 47)
        self.assertEqual(usage.context_total, 42)
        self.assertAlmostEqual(usage.cache_hit_rate, 30 / 42 * 100)
        self.assertEqual(usage.fresh, 17)
        self.assertEqual(
            usage.consumption,
            17 * models.CONSUMPTION_CONFIG.fresh_weight
            + 30 * models.CONSUMPTION_CONFIG.cache_weight,
        )

    def test_mutable_defaults_are_isolated(self):
        first = models.PromptTurn(1, "first", 1, None)
        second = models.PromptTurn(2, "second", 2, None)
        first.events.append("changed")
        first.main.add("model-a", 1, 1, 0, 0)
        self.assertEqual(second.events, [])
        self.assertEqual(second.main.total, 0)

    def test_prompt_total_includes_accounted_subsession_usage_only(self):
        prompt = models.PromptTurn(1, "delegate", 1, None)
        prompt.main.add("main", 10, 2, 0, 0)
        child = models.SubSession("child", "review", 2)
        child.usage.add("worker", 3, 4, 0, 0)
        child.analysis = models.Analysis("child.jsonl", [], models.Usage(), 0, 0, "codex")
        prompt.sub_sessions[child.key] = child
        self.assertEqual(prompt.total_usage.total, 19)

    def test_overall_excluded_count_never_goes_negative(self):
        report = models.OverallReport(
            models.Usage(), [], {}, models.Counter(),
            session_count=3, discovered_count=2, prompt_count=0, unreadable=1, days=[],
        )
        self.assertEqual(report.excluded_old, 0)


class UpdaterModuleTests(unittest.TestCase):
    @staticmethod
    def archive(entries):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as bundle:
            for name, data, kind in entries:
                info = tarfile.TarInfo(name)
                if kind == "symlink":
                    info.type, info.linkname = tarfile.SYMTYPE, data.decode()
                    bundle.addfile(info)
                else:
                    info.mode, info.size = 0o755, len(data)
                    bundle.addfile(info, io.BytesIO(data))
        return output.getvalue()

    def test_latest_release_requires_semver_tag(self):
        payload = json.dumps({"tag_name": "latest", "assets": []}).encode()
        with self.assertRaisesRegex(ValueError, "invalid release version"):
            updater.latest_release_assets("owner/repo", "1.0.0", lambda *_: payload)

    def test_latest_release_normalizes_assets(self):
        payload = json.dumps({
            "tag_name": "v1.2.3",
            "assets": [{"name": "tools-1.2.3.tar.gz", "browser_download_url": "https://asset"}],
        }).encode()
        version, assets = updater.latest_release_assets("owner/repo", "1.0.0", lambda *_: payload)
        self.assertEqual(version, "1.2.3")
        self.assertEqual(assets, {"tools-1.2.3.tar.gz": "https://asset"})

    def test_safe_extract_rejects_traversal_and_links(self):
        cases = [
            [("tools-1.2.3/../escape", b"bad", "file")],
            [("tools-1.2.3/link", b"/tmp/target", "symlink")],
        ]
        for entries in cases:
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as directory:
                archive = Path(directory) / "release.tar.gz"
                archive.write_bytes(self.archive(entries))
                with self.assertRaisesRegex(ValueError, "unsafe release archive member"):
                    updater.safe_extract_release(archive, Path(directory), "1.2.3")

    def test_update_requires_both_release_assets(self):
        with self.assertRaisesRegex(ValueError, "missing: SHA256SUMS"):
            updater.update(
                "1.0.0", lambda: ("1.1.0", {"tools-1.1.0.tar.gz": "archive"}),
                lambda *_: b"", prefix=Path("/tmp/prefix"),
            )

    def test_update_passes_verified_installer_to_runner(self):
        version = "1.1.0"
        name = f"tools-{version}.tar.gz"
        archive = self.archive([(f"tools-{version}/scripts/install.sh", b"#!/bin/sh\n", "file")])
        digest = hashlib.sha256(archive).hexdigest()
        assets = {name: "archive", "SHA256SUMS": "checksum"}
        requester = lambda url, _accept: archive if url == "archive" else f"{digest}  {name}\n".encode()
        runner = mock.Mock()
        installed = updater.update(
            "1.0.0", lambda: (version, assets), requester,
            prefix=Path("/opt/tools"), runner=runner,
        )
        self.assertEqual(installed, version)
        command = runner.call_args.args[0]
        self.assertTrue(command[0].endswith("scripts/install.sh"))
        self.assertEqual(command[1:], ["--prefix", "/opt/tools"])


class ParserModuleTests(unittest.TestCase):
    def test_compatibility_module_reexports_parser_api(self):
        self.assertIs(monitoring.create_analyzer, parsers.create_analyzer)
        self.assertIs(monitoring.ProfilerCache, parsers.ProfilerCache)
        self.assertIs(monitoring.parse_iso_timestamp, parsers.parse_iso_timestamp)

    def test_cache_round_trip_preserves_domain_types_and_internal_sets(self):
        prompt = models.PromptTurn(1, "review", 2, "2026-10-06T00:00:00Z")
        prompt.evidence_chars["repo"] = 12
        prompt.accounted_usage_keys.add("request-1")
        analysis = models.Analysis("session.jsonl", [prompt], models.Usage(), 0, 3, "codex")
        decoded = parsers.cache_decode(parsers.cache_encode(analysis))
        self.assertIsInstance(decoded, models.Analysis)
        self.assertIsInstance(decoded.prompts[0], models.PromptTurn)
        self.assertEqual(decoded.prompts[0].evidence_chars, {"repo": 12})
        self.assertEqual(decoded.prompts[0].accounted_usage_keys, {"request-1"})

    def test_analyze_counts_malformed_lines_without_losing_valid_prompt(self):
        valid = {
            "type": "user", "timestamp": "2026-10-06T00:00:00Z",
            "message": {"content": "Review"},
        }
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as session:
            session.write("not-json\n")
            session.write(json.dumps(valid) + "\n")
            path = session.name
        try:
            analysis = parsers.analyze(path)
        finally:
            os.unlink(path)
        self.assertEqual(analysis.malformed, 1)
        self.assertEqual([prompt.prompt for prompt in analysis.prompts], ["Review"])


class RefactorBoundaryTests(unittest.TestCase):
    def test_core_is_below_the_monolith_regression_limit(self):
        lines = (ROOT / "monitoring.py").read_text(encoding="utf-8").count("\n")
        self.assertLess(lines, 3200)

    def test_release_manifest_contains_every_runtime_module(self):
        manifest = runpy.run_path(str(ROOT / "scripts" / "build-release.py"))["FILES"]
        runtime_modules = {
            str(path.relative_to(ROOT)) for path in (ROOT / "agent_monitor").glob("*.py")
        }
        self.assertTrue(runtime_modules.issubset(set(manifest)))

    def test_installer_produces_a_runnable_modular_command(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "prefix"
            subprocess.run(
                [str(ROOT / "scripts" / "install.sh"), "--prefix", str(prefix)], check=True,
                capture_output=True, text=True,
            )
            result = subprocess.run(
                [str(prefix / "bin" / "agent-monitor"), "--version"], check=True,
                capture_output=True, text=True,
            )
        self.assertEqual(result.stdout.strip(), f"agent-monitor {monitoring.VERSION}")

    def test_view_helpers_are_reexported_without_wrapping(self):
        self.assertIs(monitoring.history_feed, views.history_feed)
        self.assertIs(monitoring.render_overall_html, views.render_overall_html)

    def test_ui_compatibility_classes_delegate_to_extracted_modules(self):
        self.assertTrue(issubclass(monitoring.TTYApp, tui.TTYApp))
        self.assertTrue(issubclass(monitoring.DashboardServer, dashboard.DashboardServer))


if __name__ == "__main__":
    unittest.main()
