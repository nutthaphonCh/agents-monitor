import json
import fcntl
import os
import pty
import select
import sqlite3
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
import struct
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import monitoring  # noqa: E402
sys.path.insert(0, str(ROOT / "tools"))
import codex_telemetry  # noqa: E402


def prompt(text="Run review"):
    return {
        "type": "user", "timestamp": "2026-07-20T10:00:00Z",
        "sessionId": "fixture", "message": {"content": text},
    }


def ts(value):
    """Epoch seconds for an ISO-8601 UTC timestamp, used as a mocked session file mtime."""
    return monitoring.parse_iso_timestamp(value).replace(tzinfo=monitoring.dt.timezone.utc).timestamp()


def prompt_with_cwd(cwd, text="Run review", timestamp="2026-07-20T10:00:00Z"):
    return {
        "type": "user", "timestamp": timestamp,
        "sessionId": "fixture", "cwd": cwd, "message": {"content": text},
    }


def assistant_usage(model="claude-fable", inp=10, out=5, cache_create=0, cache_read=0, timestamp="2026-07-20T10:00:01Z"):
    return {
        "type": "assistant", "timestamp": timestamp,
        "message": {
            "model": model,
            "usage": {
                "input_tokens": inp, "output_tokens": out,
                "cache_creation_input_tokens": cache_create, "cache_read_input_tokens": cache_read,
            },
            "content": [{"type": "text", "text": "ok"}],
        },
    }


def request_with_actor():
    return {
        "type": "assistant", "timestamp": "2026-07-20T10:00:01Z",
        "message": {
            "model": "claude-fable",
            "usage": {
                "input_tokens": 10, "output_tokens": 5,
                "cache_creation_input_tokens": 20, "cache_read_input_tokens": 70,
            },
            "content": [{
                "type": "tool_use", "id": "toolu_review", "name": "Bash",
                "input": {
                    "description": "Codex contract review",
                    "command": "TELEMETRY=logs/codex-last.telemetry.json codex exec --json",
                    "run_in_background": True,
                },
            }],
        },
    }


def actor_started():
    return {
        "type": "user", "timestamp": "2026-07-20T10:00:02Z",
        "message": {"content": [{
            "type": "tool_result", "tool_use_id": "toolu_review",
            "content": "Command running in background with ID: task123.",
        }]},
    }


def actor_completed():
    return {
        "type": "user", "isSynthetic": True, "timestamp": "2026-07-20T10:00:03Z",
        "message": {"content": (
            "<task-notification><task-id>task123</task-id>"
            "<tool-use-id>toolu_review</tool-use-id><status>completed</status>"
            "<summary>Background command \"Codex contract review\" completed (exit code 0)</summary>"
            "</task-notification>"
        )},
    }


def usage_observation():
    return {
        "type": "user", "timestamp": "2026-07-20T11:44:39Z",
        "message": {"content": """Usage observation

Before   18:38:11
5h       42.3%
weekly   31.1%

After    18:44:39
5h       46.8%
weekly   32.0%

Delta
5h       +4.5 pp
weekly   +0.9 pp

Confidence
high · no concurrent Claude sessions detected"""},
    }


def codex_records():
    return [
        {"type": "session_meta", "timestamp": "2026-07-20T10:00:00Z", "payload": {
            "session_id": "codex-fixture", "cwd": "/repo", "model_provider": "openai",
        }},
        {"type": "turn_context", "timestamp": "2026-07-20T10:00:00Z", "payload": {
            "turn_id": "turn-1", "model": "gpt-5.6-sol",
        }},
        {"type": "event_msg", "timestamp": "2026-07-20T10:00:00Z", "payload": {
            "type": "task_started", "turn_id": "turn-1",
        }},
        {"type": "event_msg", "timestamp": "2026-07-20T10:00:01Z", "payload": {
            "type": "item_completed", "turn_id": "turn-1", "item": {
                "type": "UserMessage", "id": "user-item-1",
                "content": [{"type": "text", "text": "Fix the profiler"}],
            },
        }},
        {"type": "response_item", "timestamp": "2026-07-20T10:00:02Z", "payload": {
            "type": "custom_tool_call", "call_id": "call-1", "name": "exec",
            "input": 'const r = await tools.exec_command({"cmd":"pytest"});',
        }},
        {"type": "response_item", "timestamp": "2026-07-20T10:00:03Z", "payload": {
            "type": "custom_tool_call_output", "call_id": "call-1", "output": "ok",
        }},
        {"type": "event_msg", "timestamp": "2026-07-20T10:00:04Z", "payload": {
            "type": "token_count", "info": {"last_token_usage": {
                "input_tokens": 100, "cached_input_tokens": 70, "output_tokens": 5,
                "reasoning_output_tokens": 2, "total_tokens": 105,
            }},
        }},
        {"type": "event_msg", "timestamp": "2026-07-20T10:00:05Z", "payload": {
            "type": "task_complete", "turn_id": "turn-1",
        }},
    ]


def telemetry_read_records():
    telemetry = {
        "kind": "codex_exec_telemetry", "thread_id": "thread-123", "exit_code": 0,
        "duration_seconds": 12.5, "event_count": 9,
        "usage": {"input_tokens": 1000, "cached_input_tokens": 800, "output_tokens": 90, "total_tokens": 1090},
        "rate_limits": {"primary": {"used_percent": 46.8, "window_minutes": 300}}, "errors": [],
    }
    return [
        {"type": "assistant", "timestamp": "2026-07-20T10:00:04Z", "message": {"content": [{
            "type": "tool_use", "id": "toolu_read_telemetry", "name": "Read",
            "input": {"file_path": "/repo/logs/codex-last.telemetry.json"},
        }]}},
        {"type": "user", "timestamp": "2026-07-20T10:00:05Z", "message": {"content": [{
            "type": "tool_result", "tool_use_id": "toolu_read_telemetry",
            "content": json.dumps(telemetry),
        }]}},
    ]


class SessionFixture:
    def __init__(self, records):
        self.temp = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        for record in records:
            self.temp.write(json.dumps(record) + "\n")
        self.temp.close()
        self.path = self.temp.name

    def append(self, record):
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    def close(self):
        os.unlink(self.path)


class SessionDiscoveryTests(unittest.TestCase):
    @staticmethod
    def write_codex_rollout(path, chat_id, source="user"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "type": "session_meta",
            "payload": {"id": chat_id, "session_id": chat_id, "thread_source": source},
        }) + "\n", encoding="utf-8")

    @staticmethod
    def create_codex_state(path):
        connection = sqlite3.connect(path)
        connection.execute("""
            CREATE TABLE threads (
                id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL,
                recency_at_ms INTEGER, recency_at INTEGER,
                updated_at_ms INTEGER, updated_at INTEGER,
                created_at_ms INTEGER, created_at INTEGER,
                archived INTEGER, thread_source TEXT, has_user_event INTEGER
            )
        """)
        return connection

    def test_codex_chats_follow_recency_and_current_rollout_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "sessions"
            old_shard = sessions / "rollout-chat-old.jsonl"
            current_shard = sessions / "rollout-chat-current.jsonl"
            newer_chat = sessions / "rollout-newer-chat.jsonl"
            subagent = sessions / "rollout-subagent.jsonl"
            self.write_codex_rollout(old_shard, "chat-one")
            self.write_codex_rollout(current_shard, "chat-one")
            self.write_codex_rollout(newer_chat, "chat-two")
            self.write_codex_rollout(subagent, "worker", "subagent")
            state = root / "state.sqlite"
            connection = self.create_codex_state(state)
            connection.executemany(
                "INSERT INTO threads VALUES (?, ?, ?, 0, 0, 0, 0, 0, 0, ?, ?)",
                [
                    ("chat-one", str(current_shard), 100_000, "user", 1),
                    ("chat-two", str(newer_chat), 200_000, "user", 1),
                    ("worker", str(subagent), 300_000, "subagent", 0),
                ],
            )
            connection.commit()
            connection.close()

            with (
                mock.patch.object(monitoring, "default_projects_dir", return_value=root / "claude"),
                mock.patch.object(monitoring, "default_codex_sessions_dir", return_value=sessions),
                mock.patch.object(monitoring, "default_codex_state_db", return_value=state),
            ):
                entries = monitoring.find_all_session_entries()

            self.assertEqual(entries, [(200.0, str(newer_chat)), (100.0, str(current_shard))])

    def test_claude_sessions_are_ordered_by_last_timestamped_record_not_mtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "claude" / "-Users-me-work-lms"
            root.mkdir(parents=True)
            old = root / "old.jsonl"
            recent = root / "recent.jsonl"
            old.write_text(
                json.dumps(prompt_with_cwd("/work/lms", timestamp="2026-07-01T09:00:00Z")) + "\n"
                + json.dumps(assistant_usage(timestamp="2026-07-01T09:01:00Z")) + "\n"
                # Bookkeeping Claude Code appends when an old transcript is listed/resumed: no timestamp.
                + json.dumps({"type": "last-prompt"}) + "\n" + json.dumps({"type": "mode"}) + "\n",
                encoding="utf-8",
            )
            recent.write_text(
                json.dumps(prompt_with_cwd("/work/lms", timestamp="2026-08-20T09:00:00Z")) + "\n"
                + json.dumps(assistant_usage(timestamp="2026-08-20T09:01:00Z")) + "\n",
                encoding="utf-8",
            )
            no_stamp = root / "no-stamp.jsonl"
            no_stamp.write_text(json.dumps({"type": "mode"}) + "\n", encoding="utf-8")
            # The stale transcript was touched most recently, yet it must not be ranked newest.
            os.utime(recent, (ts("2026-08-20T09:01:00Z"), ts("2026-08-20T09:01:00Z")))
            os.utime(old, (ts("2026-09-03T12:00:00Z"), ts("2026-09-03T12:00:00Z")))
            os.utime(no_stamp, (ts("2026-06-01T00:00:00Z"), ts("2026-06-01T00:00:00Z")))

            with (
                mock.patch.object(monitoring, "default_projects_dir", return_value=Path(directory) / "claude"),
                mock.patch.object(monitoring, "codex_chats", return_value=[]),
            ):
                entries = monitoring.find_all_session_entries()

            self.assertEqual(
                entries,
                [
                    (ts("2026-08-20T09:01:00Z"), str(recent)),
                    (ts("2026-07-01T09:01:00Z"), str(old)),
                    (ts("2026-06-01T00:00:00Z"), str(no_stamp)),  # mtime fallback
                ],
            )

    def test_codex_fallback_groups_shards_and_excludes_subagents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "sessions"
            old_shard = sessions / "rollout-chat-old.jsonl"
            current_shard = sessions / "rollout-chat-current.jsonl"
            subagent = sessions / "rollout-subagent.jsonl"
            self.write_codex_rollout(old_shard, "chat-one")
            self.write_codex_rollout(current_shard, "chat-one")
            self.write_codex_rollout(subagent, "worker", "subagent")
            os.utime(old_shard, (100, 100))
            os.utime(current_shard, (200, 200))
            os.utime(subagent, (300, 300))

            with (
                mock.patch.object(monitoring, "default_codex_sessions_dir", return_value=sessions),
                mock.patch.object(monitoring, "default_codex_state_db", return_value=root / "missing.sqlite"),
            ):
                entries = monitoring.codex_chats()

            self.assertEqual(entries, [(200, str(current_shard))])

    def test_brackets_follow_displayed_chat_indices(self):
        app = object.__new__(monitoring.TTYApp)
        app.mode = "list"
        movements = []
        app.switch_session = movements.append

        app.handle_key(ord("["))
        app.handle_key(ord("]"))

        self.assertEqual(movements, [-1, 1])

    def test_bracket_refresh_discovers_a_newer_chat_before_moving(self):
        newer = SessionFixture([prompt("Newer")])
        current = SessionFixture([prompt("Current")])
        older = SessionFixture([prompt("Older")])

        class Screen:
            def getmaxyx(self):
                return 24, 120

        try:
            app = monitoring.TTYApp(Screen(), current.path)
            app.all_session_paths = [current.path, older.path]
            app.session_paths = [current.path, older.path]
            app.session_index = 0
            with mock.patch.object(
                monitoring, "find_all_sessions",
                return_value=[newer.path, current.path, older.path],
            ):
                app.handle_key(ord("["))

            self.assertEqual(app.path, newer.path)
            self.assertEqual(app.session_index, 0)
            self.assertEqual(app.session_paths, [newer.path, current.path, older.path])
        finally:
            newer.close()
            current.close()
            older.close()


class ProfilerModelTests(unittest.TestCase):
    def setUp(self):
        self.fixture = SessionFixture([prompt(), request_with_actor(), actor_started(), actor_completed()])

    def tearDown(self):
        self.fixture.close()

    def test_actor_lifecycle_is_joined_by_tool_use_id(self):
        parser = monitoring.IncrementalSessionAnalyzer(self.fixture.path)
        actor = parser.analysis.prompts[0].actors[0]
        self.assertEqual(actor.label, "Codex contract review")
        self.assertEqual(actor.task_id, "task123")
        self.assertEqual(actor.status, "completed")
        self.assertEqual(actor.exit_code, 0)
        self.assertIsNotNone(actor.started_at)
        self.assertIsNotNone(actor.finished_at)
        timeline = [item.label for item in parser.analysis.prompts[0].timeline]
        self.assertIn("Bash · Codex contract review — started", timeline)
        self.assertIn("Bash · Codex contract review — running", timeline)
        self.assertIn("Actor · Codex contract review — completed", timeline)

    def test_foreground_agy_run_becomes_gemini_actor_without_fake_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            logs = Path(directory) / "logs"
            logs.mkdir()
            (logs / "agy-decisions.jsonl").write_text(json.dumps({
                "ts": "2026-07-20T10:00:03Z", "engine": "agy", "model": "gemini-2.5-pro",
                "mode": "review", "lane": "long-context", "decision": "Is the invariant held?",
                "verdict": "PASS", "rationale": "All call sites preserve it.",
            }) + "\n", encoding="utf-8")
            records = [prompt(), {
                "type": "assistant", "timestamp": "2026-07-20T10:00:01Z", "cwd": directory,
                "requestId": "req-agy", "message": {
                    "model": "claude-fable", "usage": {
                        "input_tokens": 1, "output_tokens": 1,
                        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 10,
                    },
                    "content": [{
                        "type": "tool_use", "id": "toolu-agy", "name": "Bash",
                        "input": {
                            "description": "Cross-model invariant review",
                            "command": (
                                "scripts/spawn/spawn-agy.sh "
                                f"--project {directory} --prompt-file /tmp/prompt "
                                "--model gemini-2.5-pro"
                            ),
                        },
                    }],
                },
            }, {
                "type": "user", "timestamp": "2026-07-20T10:00:03Z",
                "message": {"content": [{
                    "type": "tool_result", "tool_use_id": "toolu-agy", "content": (
                        f"output={directory}/logs/agy-run.md\n"
                        f"stderr={directory}/logs/agy-run.stderr\nexit_code=0\n"
                    ),
                }]},
            }]
            fixture = SessionFixture(records)
            try:
                turn = monitoring.IncrementalSessionAnalyzer(fixture.path).analysis.prompts[0]
                actor = turn.actors[0]
                self.assertEqual(actor.label, "Gemini · Cross-model invariant review")
                self.assertEqual(actor.engine, "agy")
                self.assertEqual(actor.model, "gemini-2.5-pro")
                self.assertEqual(actor.status, "completed")
                self.assertEqual(actor.exit_code, 0)
                self.assertEqual(actor.verdict, "PASS")
                self.assertEqual(actor.token_usage, {})
                detail = monitoring.actor_detail_lines(actor)
                self.assertIn("Execution", detail)
                self.assertNotIn("Response telemetry", detail)
            finally:
                fixture.close()

    def test_agy_detection_supports_direct_and_wrapper_commands(self):
        self.assertTrue(monitoring.is_agy_command("agy --mode plan -p review"))
        self.assertTrue(monitoring.is_agy_command("~/.local/bin/agy --sandbox -p review"))
        self.assertTrue(monitoring.is_agy_command("scripts/spawn/spawn-agy.sh --project /repo --prompt-file /tmp/p"))
        self.assertTrue(monitoring.is_agy_command("'/repo/scripts/spawn/spawn-agy.sh' --project '/repo' --prompt-file /tmp/p"))
        self.assertFalse(monitoring.is_agy_command("echo spawn-agy.sh"))
        self.assertFalse(monitoring.is_agy_command("agy --help"))
        self.assertFalse(monitoring.is_agy_command("bash -n scripts/spawn/spawn-agy.sh"))

    def test_slash_command_prompt_hides_xml_envelope(self):
        record = {
            "type": "user",
            "message": {"content": (
                "<command-name>/model</command-name> "
                "<command-message>model</command-message> "
                "<command-args>claude-opus-4-8</command-args>"
            )},
        }
        self.assertEqual(monitoring.prompt_text(record), "/model claude-opus-4-8")

    def test_local_command_wrappers_do_not_become_prompts(self):
        stdout = {
            "type": "user",
            "message": {"content": "<local-command-stdout>Set model to claude-opus-4-8</local-command-stdout>"},
        }
        caveat = {
            "type": "user",
            "message": {"content": (
                "<local-command-caveat>Caveat: The messages below were generated by the user "
                "while running local commands.</local-command-caveat>"
            )},
        }
        self.assertFalse(monitoring.is_real_user_prompt(stdout))
        self.assertFalse(monitoring.is_real_user_prompt(caveat))
        self.assertEqual(
            monitoring.event_label(stdout),
            "Local command · Set model to claude-opus-4-8",
        )
        self.assertIsNone(monitoring.event_label(caveat))

    def test_usage_snapshots_are_deduplicated_by_request_id(self):
        usage = {
            "input_tokens": 2, "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 900, "output_tokens": 50,
        }
        records = [prompt()]
        for block in (
            {"type": "thinking", "thinking": "..."},
            {"type": "text", "text": "working"},
            {"type": "tool_use", "id": "toolu_same", "name": "Bash", "input": {"description": "Run tests"}},
        ):
            records.append({
                "type": "assistant", "requestId": "req_same", "timestamp": "2026-07-20T10:00:01Z",
                "message": {"id": "msg_same", "model": "claude-fable", "stop_reason": "tool_use", "usage": usage, "content": [block]},
            })
        fixture = SessionFixture(records)
        try:
            turn = monitoring.IncrementalSessionAnalyzer(fixture.path).analysis.prompts[0]
            self.assertEqual(turn.main.requests, 1)
            self.assertEqual(turn.main.total, 1052)
            self.assertEqual(len(turn.requests), 1)
            self.assertEqual(turn.requests[0].actions, ["Bash · Run tests"])
        finally:
            fixture.close()

    def test_codex_response_telemetry_attaches_to_actor(self):
        for record in telemetry_read_records():
            self.fixture.append(record)
        parser = monitoring.IncrementalSessionAnalyzer(self.fixture.path)
        actor = parser.analysis.prompts[0].actors[0]
        self.assertEqual(actor.thread_id, "thread-123")
        self.assertEqual(actor.duration_seconds, 12.5)
        self.assertEqual(actor.token_usage["cached_input_tokens"], 800)
        lines = monitoring.actor_detail_lines(actor)
        self.assertIn("Response telemetry", lines)
        self.assertIn("46.8% / 300 min", "\n".join(lines))

    def test_codex_json_event_summary(self):
        events = SessionFixture([
            {"type": "thread.started", "thread_id": "thread-xyz", "timestamp": "2026-07-20T10:00:00Z"},
            {"type": "turn.completed", "timestamp": "2026-07-20T10:00:03Z", "usage": {
                "input_tokens": 500, "cached_input_tokens": 400, "output_tokens": 50,
            }},
        ])
        try:
            summary = codex_telemetry.summarize(events.path, 0)
            self.assertEqual(summary["thread_id"], "thread-xyz")
            self.assertEqual(summary["usage"]["cached_input_tokens"], 400)
            self.assertEqual(summary["duration_seconds"], 3.0)
        finally:
            events.close()

    def test_codex_legacy_user_message_event_is_still_supported(self):
        records = codex_records()[:3] + [{
            "type": "event_msg", "timestamp": "2026-07-20T10:00:01Z",
            "payload": {"type": "user_message", "message": "Legacy prompt"},
        }]
        fixture = SessionFixture(records)
        try:
            analysis = monitoring.CodexSessionAnalyzer(fixture.path).analysis
            self.assertEqual([turn.prompt for turn in analysis.prompts], ["Legacy prompt"])
        finally:
            fixture.close()

    def test_codex_rollout_normalizes_into_shared_profiler_model(self):
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", prefix="rollout-", delete=False, encoding="utf-8",
        )
        try:
            for record in codex_records():
                handle.write(json.dumps(record) + "\n")
            handle.close()
            analyzer = monitoring.create_analyzer(handle.name)
            self.assertIsInstance(analyzer, monitoring.CodexSessionAnalyzer)
            self.assertEqual(analyzer.analysis.provider, "codex")
            turn = analyzer.analysis.prompts[0]
            self.assertEqual(turn.prompt, "Fix the profiler")
            self.assertEqual(turn.main.context_total, 100)
            self.assertEqual(turn.main.output, 5)
            self.assertEqual(turn.main.requests, 1)
            self.assertEqual(turn.requests[0].actions, ["Bash · pytest"])
            self.assertEqual(turn.requests[0].action_details, ["pytest"])
            self.assertEqual(turn.requests[0].action_outputs, ["ok"])
            self.assertEqual(monitoring.main_actor_name(turn), "Codex")
            timeline = [item.label for item in turn.timeline]
            self.assertIn("Bash · pytest — started", timeline)
            self.assertIn("Bash · pytest — completed", timeline)
        finally:
            if not handle.closed:
                handle.close()
            os.unlink(handle.name)

    def test_codex_rollout_tracks_spawn_agy_wrapper_as_actor(self):
        with tempfile.TemporaryDirectory() as directory:
            records = [
                {"type": "session_meta", "timestamp": "2026-07-20T10:00:00Z", "payload": {
                    "type": "session_meta", "cwd": directory,
                }},
                {"type": "event_msg", "timestamp": "2026-07-20T10:00:01Z", "payload": {
                    "type": "user_message", "message": "Ask Gemini",
                }},
                {"type": "response_item", "timestamp": "2026-07-20T10:00:02Z", "payload": {
                    "type": "custom_tool_call", "call_id": "call-agy", "name": "exec_command",
                    "input": (
                        "const r = await tools.exec_command({"
                        f'"cmd":"scripts/spawn/spawn-agy.sh --project {directory} '
                        '--prompt-file /tmp/prompt --model gemini-test"});'
                    ),
                }},
                {"type": "response_item", "timestamp": "2026-07-20T10:00:05Z", "payload": {
                    "type": "custom_tool_call_output", "call_id": "call-agy",
                    "output": "output=/tmp/agy.md\nstderr=/tmp/agy.stderr\nexit_code=0\n",
                }},
            ]
            fixture = SessionFixture(records)
            try:
                turn = monitoring.CodexSessionAnalyzer(fixture.path).analysis.prompts[0]
                actor = turn.actors[0]
                self.assertEqual(actor.engine, "agy")
                self.assertEqual(actor.model, "gemini-test")
                self.assertEqual(actor.working_dir, directory)
                self.assertEqual(actor.status, "completed")
                self.assertEqual(actor.exit_code, 0)
                self.assertEqual(actor.token_usage, {})
            finally:
                fixture.close()

    def test_codex_exec_command_accepts_javascript_object_key(self):
        payload = {
            "input": (
                'const r = await tools.exec_command({cmd:"scripts/spawn/spawn-agy.sh '
                '--project /repo --prompt-file /tmp/prompt"});'
            ),
        }
        command = monitoring.codex_exec_command(payload)
        self.assertTrue(command.startswith("scripts/spawn/spawn-agy.sh"))
        self.assertTrue(monitoring.is_agy_command(command))

    def test_codex_wait_completes_async_agy_actor(self):
        records = [
            {"type": "event_msg", "timestamp": "2026-07-20T10:00:01Z", "payload": {
                "type": "user_message", "message": "Ask Gemini asynchronously",
            }},
            {"type": "response_item", "timestamp": "2026-07-20T10:00:02Z", "payload": {
                "type": "custom_tool_call", "call_id": "spawn", "name": "exec_command",
                "input": 'tools.exec_command({cmd:"scripts/spawn/spawn-agy.sh --project /tmp --prompt-file /tmp/p"})',
            }},
            {"type": "response_item", "timestamp": "2026-07-20T10:00:03Z", "payload": {
                "type": "custom_tool_call_output", "call_id": "spawn",
                "output": "Script running with cell ID 42\n",
            }},
            {"type": "response_item", "timestamp": "2026-07-20T10:00:04Z", "payload": {
                "type": "function_call", "call_id": "wait", "name": "wait",
                "arguments": json.dumps({"cell_id": "42"}),
            }},
            {"type": "response_item", "timestamp": "2026-07-20T10:00:05Z", "payload": {
                "type": "function_call_output", "call_id": "wait", "output": [{
                    "type": "input_text", "text": '{"exit_code":0,"output":"exit_code=0\\n"}',
                }],
            }},
        ]
        fixture = SessionFixture(records)
        try:
            actor = monitoring.CodexSessionAnalyzer(fixture.path).analysis.prompts[0].actors[0]
            self.assertEqual(actor.task_id, "42")
            self.assertEqual(actor.status, "completed")
            self.assertEqual(actor.exit_code, 0)
            self.assertEqual(actor.finished_at, "2026-07-20T10:00:05Z")
        finally:
            fixture.close()

    def test_profiler_loads_sidecar_without_transcript_tool_result(self):
        with tempfile.TemporaryDirectory() as directory:
            logs = Path(directory) / "logs"
            logs.mkdir()
            telemetry = {
                "kind": "codex_exec_telemetry", "thread_id": "sidecar-thread",
                "started_at": "2026-07-20T10:00:00Z", "finished_at": "2026-07-20T10:00:03Z",
                "duration_seconds": 3.0, "usage": {"total_tokens": 4321},
            }
            (logs / "codex-run.telemetry.json").write_text(json.dumps(telemetry), encoding="utf-8")
            actor = monitoring.Actor(
                "tool", "Codex review", finished_at="2026-07-20T10:00:04Z",
                working_dir=directory,
            )
            monitoring.load_actor_telemetry(actor)
            self.assertEqual(actor.thread_id, "sidecar-thread")
            self.assertEqual(actor.token_usage["total_tokens"], 4321)

    def test_context_attribution_has_stable_categories_and_exact_total(self):
        parser = monitoring.IncrementalSessionAnalyzer(self.fixture.path)
        turn = parser.analysis.prompts[0]
        shares = monitoring.context_attribution(turn)
        self.assertEqual(sum(value for _, value in shares), monitoring.latest_context_size(turn))
        self.assertTrue(all(value > 0 for _, value in shares))
        self.assertIn("Conversation", [name for name, _ in shares])
        lines = monitoring.detail_page_lines(turn, "context", 100)
        self.assertEqual(lines[0], "Context (approx.)")
        self.assertFalse(any(" 0" in line for line in lines[2:-2]))
        self.assertTrue(any(line.startswith("Total") for line in lines))

    def test_incremental_parser_preserves_existing_model_objects(self):
        parser = monitoring.IncrementalSessionAnalyzer(self.fixture.path)
        first_prompt = parser.analysis.prompts[0]
        old_offset = parser.offset
        self.fixture.append(prompt("Second prompt"))
        self.assertTrue(parser.poll())
        self.assertGreater(parser.offset, old_offset)
        self.assertIs(parser.analysis.prompts[0], first_prompt)
        self.assertEqual(len(parser.analysis.prompts), 2)

    def test_usage_observation_is_an_annotation_not_a_prompt(self):
        self.fixture.append(usage_observation())
        parser = monitoring.IncrementalSessionAnalyzer(self.fixture.path)
        self.assertEqual(len(parser.analysis.prompts), 1)
        observation = parser.analysis.prompts[0].usage_observations[0]
        self.assertAlmostEqual(observation.delta_5h, 4.5)
        self.assertAlmostEqual(observation.delta_weekly, 0.9)
        self.assertEqual(observation.confidence, "high")
        lines = monitoring.detail_page_lines(parser.analysis.prompts[0], "usage", 100)
        self.assertIn("+4.5 pp", "\n".join(lines))
        self.assertIn("no concurrent Claude sessions detected", "\n".join(lines))


class TTYIntegrationTests(unittest.TestCase):
    def test_app_starts_renders_and_quits_in_a_real_pty(self):
        fixture = SessionFixture([prompt(), request_with_actor(), actor_started(), actor_completed()])
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
        env = dict(os.environ, TERM="xterm-256color", PYTHONPYCACHEPREFIX="/tmp/monitoring-test-pycache")
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "monitoring.py"), fixture.path],
            stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True,
        )
        os.close(slave)
        output = b""
        def drain(seconds=0.4):
            nonlocal output
            deadline = time.time() + seconds
            while time.time() < deadline:
                ready, _, _ = select.select([master], [], [], 0.05)
                if ready:
                    try:
                        output += os.read(master, 65536)
                    except OSError:
                        break
        try:
            deadline = time.time() + 4
            while time.time() < deadline and b"Execution Profiler" not in output:
                ready, _, _ = select.select([master], [], [], 0.2)
                if ready:
                    output += os.read(master, 65536)
            os.write(master, b"\n")  # prompt overview
            drain()
            self.assertIn(b"Timeline", output)
            self.assertIn(b"Requests", output)
            os.write(master, b"\n")  # actors list
            drain()
            os.write(master, b"\x1bOB\n")  # application-mode Down, then actor detail
            drain()
            self.assertIn(b"Codex contract review", output)
            self.assertIn(b"Status", output)
            os.write(master, b"q")
            deadline = time.time() + 3
            while process.poll() is None and time.time() < deadline:
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    try:
                        output += os.read(master, 65536)
                    except OSError:
                        break
            process.wait(timeout=1)
            self.assertEqual(process.returncode, 0, output.decode("utf-8", "replace"))
            self.assertIn(b"Execution Profiler", output)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)
            fixture.close()

    def test_overall_page_opens_and_exports_in_a_real_pty(self):
        fixture = SessionFixture([prompt(), assistant_usage()])
        with tempfile.TemporaryDirectory() as fake_home:
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
            env = dict(
                os.environ, TERM="xterm-256color", HOME=fake_home,
                PYTHONPYCACHEPREFIX="/tmp/monitoring-test-pycache",
                AGENT_MONITOR_NO_BROWSER="1",
            )
            process = subprocess.Popen(
                [sys.executable, str(ROOT / "monitoring.py"), fixture.path],
                stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True,
            )
            os.close(slave)
            output = b""

            def drain(seconds=0.4):
                nonlocal output
                deadline = time.time() + seconds
                while time.time() < deadline:
                    ready, _, _ = select.select([master], [], [], 0.05)
                    if ready:
                        try:
                            output += os.read(master, 65536)
                        except OSError:
                            break

            try:
                deadline = time.time() + 4
                while time.time() < deadline and b"Execution Profiler" not in output:
                    ready, _, _ = select.select([master], [], [], 0.2)
                    if ready:
                        output += os.read(master, 65536)
                os.write(master, b"o")  # open the Overall page — no ~/.claude or ~/.codex under fake_home
                drain(1.0)
                self.assertIn(b"Overall consumption across discovered sessions", output)
                self.assertIn(b"Not measured", output)
                os.write(master, b"p")  # export the HTML report
                drain(0.6)
                self.assertIn(b"saved", output)
                report_path = Path(fake_home) / "Library" / "Caches" / "execution-profiler" / "overall-report.html"
                self.assertTrue(report_path.is_file())
                self.assertIn("<!doctype html>", report_path.read_text(encoding="utf-8"))
                os.write(master, b"q")
                deadline = time.time() + 3
                while process.poll() is None and time.time() < deadline:
                    ready, _, _ = select.select([master], [], [], 0.1)
                    if ready:
                        try:
                            output += os.read(master, 65536)
                        except OSError:
                            break
                process.wait(timeout=1)
                self.assertEqual(process.returncode, 0, output.decode("utf-8", "replace"))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                os.close(master)
                fixture.close()

    def test_sqlite_cache_restores_state_and_ingests_only_appended_records(self):
        fixture = SessionFixture([prompt()])
        cache_file = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        cache_file.close()
        try:
            cache = monitoring.ProfilerCache(cache_file.name)
            first = cache.analyzer(fixture.path)
            self.assertEqual(len(first.analysis.prompts), 1)
            cached_offset = first.offset

            fixture.append(request_with_actor())
            resumed = cache.analyzer(fixture.path)
            self.assertGreater(resumed.offset, cached_offset)
            self.assertEqual(len(resumed.analysis.prompts), 1)
            self.assertEqual(resumed.analysis.prompts[0].main.requests, 1)

            row = cache.db.execute(
                "SELECT byte_offset FROM materialized_sessions WHERE source_path = ?",
                (str(Path(fixture.path).resolve()),),
            ).fetchone()
            self.assertEqual(row[0], os.path.getsize(fixture.path))
            cache.db.close()
        finally:
            fixture.close()
            os.unlink(cache_file.name)

    def test_sqlite_cache_restores_codex_adapter_type(self):
        session = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", prefix="rollout-", delete=False, encoding="utf-8",
        )
        cache_file = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        cache_file.close()
        try:
            for record in codex_records():
                session.write(json.dumps(record) + "\n")
            session.close()
            cache = monitoring.ProfilerCache(cache_file.name)
            first = cache.analyzer(session.name)
            second = cache.analyzer(session.name)
            self.assertIsInstance(first, monitoring.CodexSessionAnalyzer)
            self.assertIsInstance(second, monitoring.CodexSessionAnalyzer)
            self.assertEqual(second.analysis.prompts[0].main.context_total, 100)
            self.assertEqual(second.analysis.prompts[0].requests[0].action_outputs, ["ok"])
            cache.db.close()
        finally:
            if not session.closed:
                session.close()
            os.unlink(session.name)
            os.unlink(cache_file.name)


class FrameAffordanceTests(unittest.TestCase):
    def test_chat_header_uses_navigation_arrows_instead_of_position_counts(self):
        app = object.__new__(monitoring.TTYApp)
        app.analysis = monitoring.Analysis(
            "/tmp/rollout-current.jsonl", [], monitoring.Usage(), 0, 0, provider="codex",
        )
        app.path = app.analysis.path
        app.status = "20:06:31"
        app.session_paths = [app.path, "/tmp/rollout-older.jsonl"]
        app.session_index = 0

        header = app.session_header(120)

        self.assertTrue(header.startswith("< · Codex · rollout-current"))
        self.assertTrue(header.endswith("· 20:06:31 · >"))
        self.assertNotIn("Chat 1/", header)
        self.assertEqual(app.session_navigation_availability(), (False, True))
        app.session_index = 1
        self.assertEqual(app.session_navigation_availability(), (True, False))

    def test_codex_chat_header_uses_short_rollout_display_id(self):
        path = (
            "/tmp/rollout-2026-09-03T16-30-06-"
            "01a0669a-a7a5-71f1-8431-59a6fa3c6ce0.jsonl"
        )
        app = object.__new__(monitoring.TTYApp)
        app.analysis = monitoring.Analysis(
            path, [], monitoring.Usage(), 0, 0, provider="codex",
        )
        app.path = path
        app.status = "20:06:31"

        header = app.session_header(120)

        self.assertIn(
            "rollout-2026-09-03T16-30-06-01a0669a-a7a5-71f1-8431",
            header,
        )
        self.assertNotIn("59a6fa3c6ce0", header)

    def test_process_clock_renders_local_started_and_finished_times(self):
        timestamp = "2026-07-20T10:11:12+00:00"
        expected = monitoring.parse_iso_timestamp(timestamp).astimezone().strftime("%H:%M:%S")
        self.assertEqual(monitoring.process_clock(timestamp), expected)
        self.assertEqual(monitoring.process_clock(None), "--:--:--")

    def test_process_view_is_scoped_to_selected_prompt(self):
        first = monitoring.PromptTurn(1, "first", 1, "2026-07-20T10:00:00Z")
        first.actors.append(monitoring.Actor("a", "Claude one", "completed", engine="claude"))
        second = monitoring.PromptTurn(2, "second", 2, "2026-07-20T10:01:00Z")
        second.actors.append(monitoring.Actor("b", "Gemini two", "running", engine="agy"))
        analysis = monitoring.Analysis(
            "/tmp/session.jsonl", [first, second], monitoring.Usage(), 0, 2, provider="codex",
        )
        self.assertEqual([actor.key for _, actor in monitoring.session_processes(analysis, 1)], ["a"])
        self.assertEqual([actor.key for _, actor in monitoring.session_processes(analysis, 2)], ["b"])

    def test_yielded_shell_commands_are_not_background_agents(self):
        records = [
            codex_records()[0], codex_records()[1], codex_records()[2], codex_records()[3],
            {"type": "response_item", "timestamp": "2026-07-20T10:00:02Z", "payload": {
                "type": "custom_tool_call", "call_id": "shell-call", "name": "exec",
                "input": 'const r = await tools.exec_command({"cmd":"python3 -m unittest"});',
            }},
            {"type": "response_item", "timestamp": "2026-07-20T10:00:03Z", "payload": {
                "type": "custom_tool_call_output", "call_id": "shell-call",
                "output": "Script running with cell ID cell-shell",
            }},
        ]
        fixture = SessionFixture(records)
        try:
            analysis = monitoring.CodexSessionAnalyzer(fixture.path).analysis
            self.assertEqual(monitoring.session_processes(analysis), [])
        finally:
            fixture.close()

    def test_spawn_engine_requires_execution_not_a_script_reference(self):
        self.assertIsNone(monitoring.spawned_agent_engine("chmod +x scripts/spawn/spawn-claude.sh"))
        self.assertIsNone(monitoring.spawned_agent_engine("bash -n scripts/spawn/spawn-codex.sh"))
        self.assertIsNone(monitoring.spawned_agent_engine("rg spawn-claude.sh scripts"))
        self.assertIsNone(monitoring.spawned_agent_engine("scripts/spawn/spawn-codex.sh --help"))
        self.assertIsNone(monitoring.spawned_agent_engine("scripts/spawn/spawn-claude.sh --dry-run"))
        self.assertEqual(
            monitoring.spawned_agent_engine("bash scripts/spawn/spawn-claude.sh --project /repo"),
            "claude",
        )

    def test_p_opens_session_processes_and_realtime_detail(self):
        session = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", prefix="rollout-", delete=False, encoding="utf-8",
        )
        result = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8")
        result.write("first response line\nsecond response line\n")
        result.close()
        records = [
            codex_records()[0], codex_records()[1], codex_records()[2], codex_records()[3],
            {"type": "response_item", "timestamp": "2026-07-20T10:00:02Z", "payload": {
                "type": "custom_tool_call", "call_id": "bg-call", "name": "exec",
                "input": 'const r = await tools.exec_command({"cmd":"scripts/spawn/spawn-claude.sh --project /repo"});',
            }},
            {"type": "response_item", "timestamp": "2026-07-20T10:00:03Z", "payload": {
                "type": "custom_tool_call_output", "call_id": "bg-call",
                "output": "Script running with cell ID cell-42",
            }},
        ]
        try:
            for record in records:
                session.write(json.dumps(record) + "\n")
            session.close()

            class Screen:
                def getmaxyx(self):
                    return 24, 120

            app = monitoring.TTYApp(Screen(), session.name)
            app.refresh(force=True)
            app.handle_key(ord("p"))
            self.assertEqual(app.mode, "processes")
            rows = [line for line, _ in app.frame()]
            self.assertTrue(any("1 active" in line for line in rows))
            self.assertTrue(any("claude" in line and "running" in line for line in rows))
            app.handle_key(monitoring.curses.KEY_RIGHT)
            self.assertEqual(app.mode, "process_detail")

            with open(session.name, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "type": "response_item", "timestamp": "2026-07-20T10:00:04Z",
                    "payload": {"type": "function_call", "call_id": "wait-call", "name": "wait",
                                "arguments": json.dumps({"cell_id": "cell-42"})},
                }) + "\n")
                fh.write(json.dumps({
                    "type": "response_item", "timestamp": "2026-07-20T10:00:05Z",
                    "payload": {"type": "function_call_output", "call_id": "wait-call",
                                "output": f"output={result.name}\nexit_code=0"},
                }) + "\n")
            app.refresh(force=True)
            prompt, actor = app.selected_process()
            self.assertEqual(actor.status, "completed")
            self.assertEqual(actor.exit_code, 0)
            self.assertEqual(actor.output_path, result.name)
            detail = [line for line, _ in app.frame()]
            self.assertTrue(any("second response line" in line for line in detail))
            app.handle_key(9)
            self.assertEqual(app.process_tab, "logs")
            app.handle_key(9)
            self.assertEqual(app.process_tab, "metadata")
        finally:
            if not session.closed:
                session.close()
            os.unlink(session.name)
            os.unlink(result.name)

    def test_search_matches_are_remapped_when_switching_views(self):
        records = []
        for index in range(6):
            records.extend([prompt(f"Prompt {index}"), request_with_actor()])
        fixture = SessionFixture(records)
        class Screen:
            def getmaxyx(self):
                return 24, 120
        try:
            app = monitoring.TTYApp(Screen(), fixture.path)
            app.refresh(force=True)
            app.search_query = "thinking rounds"
            app.update_search()
            self.assertTrue(any(index >= len(app.history) for index in app.search_matches))
            app.handle_key(ord("2"))
            self.assertTrue(all(index < len(app.history) for index in app.search_matches))
            for _ in range(20):
                app.handle_key(ord("n"))
            app.handle_key(monitoring.curses.KEY_RIGHT)
            self.assertEqual(app.mode, "detail")
        finally:
            fixture.close()

    def test_z_x_filter_provider_and_return_to_previous_session(self):
        claude = SessionFixture([prompt()])
        codex = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", prefix="rollout-", delete=False, encoding="utf-8",
        )
        class Screen:
            def getmaxyx(self):
                return 24, 120
        try:
            for record in codex_records():
                codex.write(json.dumps(record) + "\n")
            codex.close()
            app = monitoring.TTYApp(Screen(), claude.path)
            app.all_session_paths = [claude.path, codex.name]
            app.session_paths = [claude.path]
            app.session_index = 0
            app.provider_positions = {"claude": claude.path}
            app.handle_key(ord("x"))
            self.assertEqual(app.analysis.provider, "codex")
            self.assertEqual(app.path, codex.name)
            self.assertEqual(app.session_paths, [codex.name])
            app.handle_key(ord("z"))
            self.assertEqual(app.analysis.provider, "claude")
            self.assertEqual(app.path, claude.path)
            self.assertEqual(app.session_paths, [claude.path])
        finally:
            claude.close()
            if not codex.closed:
                codex.close()
            os.unlink(codex.name)

    def test_codex_session_renders_through_existing_list_and_detail_routes(self):
        session = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", prefix="rollout-", delete=False, encoding="utf-8",
        )
        class Screen:
            def getmaxyx(self):
                return 24, 120
        try:
            for record in codex_records():
                session.write(json.dumps(record) + "\n")
            session.close()
            app = monitoring.TTYApp(Screen(), session.name)
            app.refresh(force=True)
            rows = [row for row, _ in app.frame()]
            self.assertTrue(any("· Codex · rollout-" in row for row in rows))
            self.assertTrue(any('1 | 10:00 · "Fix the profiler"' in row for row in rows))
            app.handle_key(monitoring.curses.KEY_RIGHT)
            overview = [row for row, _ in app.frame()]
            self.assertTrue(any("Actors    Codex" in row for row in overview))
        finally:
            if not session.closed:
                session.close()
            os.unlink(session.name)

    def test_request_rows_use_aligned_profiler_columns(self):
        fixture = SessionFixture([prompt(), request_with_actor()])
        try:
            turn = monitoring.IncrementalSessionAnalyzer(fixture.path).analysis.prompts[0]
            lines = monitoring.detail_page_lines(turn, "requests", 120)
            request_line = next(line for line in lines if "[ fable ]" in line)
            self.assertRegex(
                request_line,
                r"^1 \| 10:00 · \[ fable \] · ↓ 100 \( 70% cached\) · ↑ 5$",
            )
            action_line = next(line for line in lines if "→ Bash" in line)
            self.assertNotIn("|", action_line)
            self.assertTrue(action_line.startswith("    → "))

            class Screen:
                def getmaxyx(self):
                    return 24, 120

            app = monitoring.TTYApp(Screen(), fixture.path)
            app.refresh(force=True)
            app.inspect()
            app.detail_page = "requests"
            rendered_action = next(row for row, _ in app.frame() if "→ Bash" in row)
            self.assertTrue(rendered_action.startswith("      → "))
        finally:
            fixture.close()

    def test_enter_on_request_opens_full_command_detail(self):
        fixture = SessionFixture([prompt(), request_with_actor(), actor_started()])
        try:
            class Screen:
                def getmaxyx(self):
                    return 24, 64

            app = monitoring.TTYApp(Screen(), fixture.path)
            app.refresh(force=True)
            app.inspect()
            app.detail_page = "requests"
            app.handle_key(10)
            self.assertEqual(app.detail_page, "request:0")
            detail = "\n".join(row for row, _ in app.frame())
            self.assertIn("10:00 · fable · ↓ 100 (70% cached) · ↑ 5", detail)
            self.assertIn(
                "$ TELEMETRY=logs/codex-last.telemetry.json codex exec --json",
                " ".join(detail.split()),
            )
            self.assertIn("Output", detail)
            self.assertIn("Command running in background with ID: task123.", detail)
            app.handle_key(monitoring.curses.KEY_LEFT)
            self.assertEqual(app.detail_page, "requests")
        finally:
            fixture.close()

    @mock.patch("monitoring.subprocess.run")
    def test_y_copies_full_request_commands(self, run):
        fixture = SessionFixture([prompt(), request_with_actor()])
        try:
            class Screen:
                def getmaxyx(self):
                    return 24, 80

            app = monitoring.TTYApp(Screen(), fixture.path)
            app.refresh(force=True)
            app.inspect()
            app.detail_page = "request:0"
            app.handle_key(ord("y"))
            run.assert_called_once_with(
                ("pbcopy",),
                input="TELEMETRY=logs/codex-last.telemetry.json codex exec --json",
                text=True,
                check=True,
            )
            self.assertEqual(app.clipboard_notice, "copied")
        finally:
            fixture.close()

    def test_file_paths_are_located_for_dim_rendering(self):
        cases = {
            "     → Write · /repo/spec.py": "/repo/spec.py",
            "18:40  Write   /repo/spec.py": "/repo/spec.py",
            "18:40 Bash · Run tests — completed": None,
        }
        for rendered, expected in cases.items():
            with self.subTest(rendered=rendered):
                span = monitoring.file_path_span(rendered)
                actual = rendered[slice(*span)] if span else None
                self.assertEqual(actual, expected)

    def test_only_tool_action_label_is_located_for_color(self):
        cases = {
            "      → Write · /repo/spec.py": "Write",
            "18:40  edit    /repo/spec.py": "edit",
            "18:40 Bash · Run tests — completed": "Bash",
            "      → WebSearch · Find API docs": "WebSearch",
            "Actors": None,
        }
        for rendered, expected in cases.items():
            with self.subTest(rendered=rendered):
                span = monitoring.action_span(rendered)
                actual = rendered[slice(*span)] if span else None
                self.assertEqual(actual, expected)

    def test_only_enterable_rows_get_chevrons(self):
        fixture = SessionFixture([prompt(), request_with_actor(), actor_started(), actor_completed()])
        class Screen:
            def getmaxyx(self):
                return 24, 100
        try:
            app = monitoring.TTYApp(Screen(), fixture.path)
            app.refresh(force=True)
            live = [row[0] for row in app.frame()[2:-1] if row[0].strip()]
            prompt_row = next(row for row in live if '1 | 10:00 · "Run review"' in row)
            self.assertTrue(prompt_row.startswith("> "))
            self.assertFalse(next(row for row in live if "Subtask task123" in row).startswith("> "))
            app.handle_key(monitoring.curses.KEY_RIGHT)
            self.assertEqual(app.mode, "detail")
            detail = [row[0] for row in app.frame()[2:-1] if row[0].strip()]
            self.assertTrue(any("Overview" in row for row in detail))
            self.assertTrue(any("I/O" in row and "cached" in row for row in detail))
            self.assertTrue(any("thinking rounds" in row for row in detail))
            self.assertTrue(any("file ops" in row for row in detail))
            self.assertTrue(any(row.startswith("> Actors") for row in detail))
            self.assertTrue(any(row.startswith("> Timeline") for row in detail))
            app.handle_key(monitoring.curses.KEY_LEFT)
            self.assertEqual(app.mode, "list")
        finally:
            fixture.close()


class OverallPageTests(unittest.TestCase):
    def test_usage_tracks_raw_telemetry_and_weighted_consumption_per_model(self):
        usage = monitoring.Usage()
        usage.add("model-a", 10, 5, 2, 3)
        usage.add("model-b", 1, 1, 0, 0)
        usage.add("model-a", 4, 4, 0, 0)
        self.assertEqual(usage.model_totals["model-a"], 10 + 5 + 2 + 3 + 4 + 4)
        self.assertEqual(usage.model_totals["model-b"], 2)
        self.assertEqual(sum(usage.model_totals.values()), usage.total)
        self.assertAlmostEqual(usage.consumption, usage.fresh + usage.cache_read * 0.1)
        self.assertAlmostEqual(
            sum(usage.model_consumption.values()), usage.consumption,
        )

        other = monitoring.Usage()
        other.add("model-a", 1, 1, 1, 1)
        usage.merge(other)
        self.assertEqual(usage.model_totals["model-a"], 10 + 5 + 2 + 3 + 4 + 4 + 4)
        self.assertEqual(sum(usage.model_totals.values()), usage.total)

    def test_consumption_weight_config_rejects_invalid_values(self):
        with mock.patch.dict(os.environ, {"WEIGHT": "0.25"}):
            self.assertEqual(monitoring.configured_weight("WEIGHT", 1.0), 0.25)
        with mock.patch.dict(os.environ, {"WEIGHT": "invalid"}):
            self.assertEqual(monitoring.configured_weight("WEIGHT", 1.0), 1.0)
        with mock.patch.dict(os.environ, {"WEIGHT": "-1"}):
            self.assertEqual(monitoring.configured_weight("WEIGHT", 1.0), 1.0)

    def test_session_cache_percentage_uses_every_request_not_only_latest(self):
        fixture = SessionFixture([
            prompt("First"),
            assistant_usage(inp=100, out=0, cache_read=900),
            {**prompt("Second"), "timestamp": "2026-07-20T10:01:00Z"},
            assistant_usage(inp=1_000, out=0, cache_read=0, timestamp="2026-07-20T10:01:01Z"),
        ])
        try:
            analysis = monitoring.create_analyzer(fixture.path).analysis
            latest, peak, cache_rate = monitoring.session_context_summary([analysis])
            self.assertEqual(latest, 1_000)
            self.assertEqual(peak, 1_000)
            self.assertEqual(cache_rate, 45.0)
        finally:
            fixture.close()

    def test_consumption_ranking_discounts_cache_reads(self):
        project = monitoring.ProjectUsage("weighted")
        cache_heavy = monitoring.Usage()
        cache_heavy.add("opus-5", 50_000_000, 5_000_000, 0, 250_000_000)
        fresh_heavy = monitoring.Usage()
        fresh_heavy.add("fable-5", 130_000_000, 10_000_000, 0, 0)
        project.usage.merge(cache_heavy)
        project.usage.merge(fresh_heavy)
        project.recent_sessions.extend([
            monitoring.SessionUsage(
                "cache-heavy", "", 1, "Cache-heavy", "2026-09-03", "claude", cache_heavy,
            ),
            monitoring.SessionUsage(
                "fresh-heavy", "", 1, "Fresh-heavy", "2026-09-03", "claude", fresh_heavy,
            ),
        ])

        lines = monitoring.project_detail_lines(project, 120)
        ranked = [line for line in lines if line.startswith(("1. ", "2. "))]

        self.assertAlmostEqual(cache_heavy.consumption, 80_000_000)
        self.assertAlmostEqual(fresh_heavy.consumption, 140_000_000)
        self.assertIn("Fresh-heavy", ranked[0])
        self.assertIn("Cache-heavy", ranked[1])

    def test_build_overall_report_groups_projects_providers_and_models(self):
        claude_alpha = SessionFixture([
            prompt_with_cwd("/work/alpha", timestamp="2026-07-20T09:00:00Z"),
            assistant_usage(model="claude-sonnet-5", inp=100, out=50, cache_read=200, timestamp="2026-07-20T09:00:01Z"),
        ])
        claude_beta = SessionFixture([
            prompt_with_cwd("/work/beta", timestamp="2026-07-21T09:00:00Z"),
            assistant_usage(model="claude-sonnet-5", inp=10, out=5, timestamp="2026-07-21T09:00:01Z"),
        ])
        codex_repo = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", prefix="rollout-", delete=False, encoding="utf-8",
        )
        for record in codex_records():
            codex_repo.write(json.dumps(record) + "\n")
        codex_repo.close()
        try:
            with mock.patch.object(
                monitoring, "find_all_session_entries",
                return_value=[
                    (ts("2026-07-21T09:00:00Z"), claude_alpha.path),
                    (ts("2026-07-21T09:00:00Z"), claude_beta.path),
                    (ts("2026-07-20T09:00:00Z"), codex_repo.name),
                ],
            ):
                report = monitoring.build_overall_report(
                    None, now=monitoring.dt.datetime(2026, 8, 1, tzinfo=monitoring.dt.timezone.utc),
                )

            self.assertEqual(report.discovered_count, 3)
            self.assertEqual(report.session_count, 3)
            self.assertEqual(report.unreadable, 0)
            self.assertEqual(report.prompt_count, 3)

            self.assertEqual({project.name for project in report.projects}, {"alpha", "beta", "repo"})
            self.assertEqual(report.projects[0].name, "alpha")  # ranked by weighted consumption
            self.assertEqual(report.projects[0].sessions, 1)
            self.assertEqual(len(report.projects[0].recent_sessions), 1)
            self.assertEqual(report.projects[0].recent_sessions[0].label, "Run review")
            self.assertEqual(report.projects[0].recent_sessions[0].session_id, "fixture")
            self.assertGreater(report.projects[0].recent_sessions[0].latest_context, 0)
            self.assertGreaterEqual(
                report.projects[0].recent_sessions[0].peak_context,
                report.projects[0].recent_sessions[0].latest_context,
            )
            self.assertEqual(
                sum(report.projects[0].usage.model_totals.values()), report.projects[0].usage.total,
            )

            self.assertGreater(report.provider_usage["claude"].total, 0)
            self.assertGreater(report.provider_usage["codex"].total, 0)
            self.assertEqual(report.provider_sessions["claude"], 2)
            self.assertEqual(report.provider_sessions["codex"], 1)

            self.assertIn("claude-sonnet-5", report.total.model_totals)
            self.assertIn("gpt-5.6-sol", report.total.model_totals)
            self.assertEqual(sum(report.total.model_totals.values()), report.total.total)
            self.assertEqual({day.day for day in report.days}, {"2026-07-20", "2026-07-21"})
            codex_session = next(project for project in report.projects if project.name == "repo").recent_sessions[0]
            self.assertEqual(codex_session.session_id, "codex-fixture")
            self.assertTrue(codex_session.rollout_id.startswith("rollout-"))
            self.assertEqual(codex_session.rollout_count, 1)
        finally:
            claude_alpha.close()
            claude_beta.close()
            os.unlink(codex_repo.name)

    def test_build_overall_report_counts_unreadable_sessions_without_dropping_totals(self):
        good = SessionFixture([prompt_with_cwd("/work/good"), assistant_usage()])
        missing_path = good.path + ".missing"
        try:
            with mock.patch.object(
                monitoring, "find_all_session_entries",
                return_value=[(time.time(), good.path), (time.time(), missing_path)],
            ):
                report = monitoring.build_overall_report(None)
            self.assertEqual(report.discovered_count, 2)
            self.assertEqual(report.session_count, 1)
            self.assertEqual(report.unreadable, 1)
            self.assertEqual(report.excluded_old, 0)
            self.assertGreater(report.total.total, 0)
            self.assertNotIn("older than", "\n".join(monitoring.overall_lines(report, 100)))
        finally:
            good.close()

    def test_project_sessions_are_limited_to_last_30_days(self):
        recent = SessionFixture([
            prompt_with_cwd("/work/lms", text="Recent session", timestamp="2026-08-20T09:00:00Z"),
            assistant_usage(inp=100, out=10, timestamp="2026-08-20T09:01:00Z"),
        ])
        old = SessionFixture([
            prompt_with_cwd("/work/lms", text="Old session", timestamp="2026-07-01T09:00:00Z"),
            assistant_usage(inp=500, out=10, timestamp="2026-07-01T09:01:00Z"),
        ])
        try:
            with mock.patch.object(
                monitoring, "find_all_session_entries",
                return_value=[(ts("2026-08-20T09:01:00Z"), recent.path), (ts("2026-07-01T09:01:00Z"), old.path)],
            ):
                report = monitoring.build_overall_report(
                    None, now=monitoring.dt.datetime(2026, 9, 3, tzinfo=monitoring.dt.timezone.utc),
                )
            project = report.projects[0]
            self.assertEqual(project.sessions, 2)
            self.assertEqual([item.label for item in project.recent_sessions], ["Recent session"])
            self.assertEqual(project.recent_sessions[0].prompt_count, 1)
        finally:
            recent.close()
            old.close()

    def test_build_overall_report_skips_sessions_older_than_window_without_parsing(self):
        recent = SessionFixture([
            prompt_with_cwd("/work/lms", timestamp="2026-08-20T09:00:00Z"),
            assistant_usage(inp=100, out=10, timestamp="2026-08-20T09:01:00Z"),
        ])
        stale = SessionFixture([
            prompt_with_cwd("/work/lms", timestamp="2026-05-01T09:00:00Z"),
            assistant_usage(inp=500, out=10, timestamp="2026-05-01T09:01:00Z"),
        ])
        now = monitoring.dt.datetime(2026, 9, 3, tzinfo=monitoring.dt.timezone.utc)
        try:
            with mock.patch.object(
                monitoring, "find_all_session_entries",
                return_value=[(ts("2026-08-20T09:01:00Z"), recent.path), (ts("2026-05-01T09:01:00Z"), stale.path)],
            ), mock.patch.object(
                monitoring, "create_analyzer", wraps=monitoring.create_analyzer,
            ) as analyzer_calls:
                report = monitoring.build_overall_report(None, now=now)
                wide = monitoring.build_overall_report(None, window_days=365, now=now)
            self.assertEqual(report.discovered_count, 2)
            self.assertEqual(report.session_count, 1)
            self.assertEqual(report.excluded_old, 1)
            self.assertEqual(report.total.total, 110)
            self.assertEqual(
                [call.args[0] for call in analyzer_calls.call_args_list],
                [recent.path, recent.path, stale.path],  # 90-day run parsed only `recent`; 365-day run parsed both
            )
            lines = "\n".join(monitoring.overall_lines(report, 100))
            self.assertIn("Sessions active in the last 90 days", lines)
            self.assertIn("1 older than 90 days, excluded", lines)
            self.assertIn("older than 90 days", monitoring.render_overall_html(report, "2026-09-03"))
            self.assertEqual(wide.session_count, 2)
            self.assertEqual(wide.excluded_old, 0)
        finally:
            recent.close()
            stale.close()

    def test_projects_are_grouped_by_full_cwd_and_same_basenames_are_disambiguated(self):
        work_tools = SessionFixture([prompt_with_cwd("/Users/me/work/tools"), assistant_usage(inp=300)])
        lg_tools = SessionFixture([prompt_with_cwd("/Users/me/work/lg/tools"), assistant_usage(inp=100)])
        other = SessionFixture([prompt_with_cwd("/Users/me/work/flood"), assistant_usage(inp=10)])
        try:
            with mock.patch.object(
                monitoring, "find_all_session_entries",
                return_value=[(time.time(), path) for path in (work_tools.path, lg_tools.path, other.path)],
            ):
                report = monitoring.build_overall_report(None)
            self.assertEqual(
                [(project.name, project.root, project.sessions) for project in report.projects],
                [
                    ("work/tools", "/Users/me/work/tools", 1),
                    ("lg/tools", "/Users/me/work/lg/tools", 1),
                    ("flood", "/Users/me/work/flood", 1),
                ],
            )
            html_text = monitoring.render_overall_html(report, "now")
            self.assertIn('title="/Users/me/work/lg/tools">lg/tools', html_text)
        finally:
            work_tools.close()
            lg_tools.close()
            other.close()

    def test_scratchpad_cwd_is_attributed_to_the_owning_sessions_project(self):
        owner_id = "8e4025bf-d06a-45e3-a7ff-59027338c78d"
        with tempfile.TemporaryDirectory() as projects_dir:
            project_dir = Path(projects_dir) / "-Users-me-work-lms"
            project_dir.mkdir()
            (project_dir / f"{owner_id}.jsonl").write_text(
                json.dumps(prompt_with_cwd("/Users/me/work/lms")) + "\n", encoding="utf-8",
            )
            scratch_cwd = f"/private/tmp/claude-501/-Users-me-work-lms/{owner_id}/scratchpad"
            spawned = SessionFixture([prompt_with_cwd(scratch_cwd), assistant_usage(inp=50)])
            orphan_cwd = "/private/tmp/claude-501/-Users-me-work-gone/00000000-0000-0000-0000-000000000000/scratchpad"
            orphan = SessionFixture([prompt_with_cwd(orphan_cwd), assistant_usage(inp=5)])
            try:
                with mock.patch.object(monitoring, "default_projects_dir", return_value=Path(projects_dir)):
                    self.assertEqual(monitoring.session_project_root(spawned.path), "/Users/me/work/lms")
                    # An owner that no longer exists keeps the scratchpad path rather than guessing.
                    self.assertEqual(monitoring.session_project_root(orphan.path), orphan_cwd)
            finally:
                spawned.close()
                orphan.close()

    def test_daily_trend_buckets_use_the_viewers_timezone(self):
        late = SessionFixture([
            prompt_with_cwd("/work/lms", timestamp="2026-07-20T20:30:00Z"),
            assistant_usage(inp=100, out=10, timestamp="2026-07-20T20:31:00Z"),
        ])
        bangkok = monitoring.dt.timezone(monitoring.dt.timedelta(hours=7))
        now = monitoring.dt.datetime(2026, 8, 1, tzinfo=monitoring.dt.timezone.utc)
        try:
            with mock.patch.object(
                monitoring, "find_all_session_entries", return_value=[(ts("2026-07-20T20:31:00Z"), late.path)],
            ):
                local = monitoring.build_overall_report(None, now=now, tz=bangkok)
                utc = monitoring.build_overall_report(None, now=now, tz=monitoring.dt.timezone.utc)
            self.assertEqual([day.day for day in local.days], ["2026-07-21"])
            self.assertEqual([day.day for day in utc.days], ["2026-07-20"])
            self.assertTrue(local.projects[0].recent_sessions[0].timestamp.startswith("2026-07-21T03:30"))
        finally:
            late.close()

    def test_codex_rollout_shards_are_one_session_and_their_usage_is_combined(self):
        thread_id = "01a06201-c11e-7430-9dc1-57700eb22393"
        with tempfile.TemporaryDirectory() as tmp_dir:
            first = Path(tmp_dir) / f"rollout-2026-07-20T10-00-00-{thread_id}.jsonl"
            current = Path(tmp_dir) / (
                f"rollout-2026-07-20T11-00-00-{thread_id}_"
                "01a0624a-fb7e-7531-a77e-de8c2abc4417.jsonl"
            )
            for path in (first, current):
                records = codex_records()
                records[0]["payload"]["id"] = thread_id
                records[0]["payload"]["session_id"] = thread_id
                path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

            one_shard_total = monitoring.create_analyzer(str(first)).analysis.total_usage.total
            with mock.patch.object(
                monitoring, "find_all_session_entries", return_value=[(ts("2026-07-20T11:00:00Z"), str(current))],
            ):
                report = monitoring.build_overall_report(
                    None, now=monitoring.dt.datetime(2026, 8, 1, tzinfo=monitoring.dt.timezone.utc),
                )

            self.assertEqual(report.session_count, 1)
            self.assertEqual(report.provider_sessions["codex"], 1)
            self.assertEqual(report.prompt_count, 2)
            self.assertEqual(report.total.total, one_shard_total * 2)
            session = report.projects[0].recent_sessions[0]
            self.assertEqual(session.session_id, thread_id)
            self.assertEqual(session.rollout_id, current.stem)
            self.assertEqual(session.rollout_count, 2)
            html_text = monitoring.render_overall_html(report, "2026-08-01 00:00:00")
            display_id = (
                "rollout-2026-07-20T11-00-00-01a06201-c11e-7430-9dc1_"
                "01a0624a-fb7e-7531-a77e"
            )
            self.assertIn(display_id, html_text)
            self.assertIn(f"thread {thread_id}", html_text)
            self.assertIn(f"full rollout {current.stem}", html_text)
            self.assertIn("2 shard(s)", html_text)

    def test_codex_rollout_display_id_strips_each_uuid_tail(self):
        root = "rollout-2026-09-03T16-30-06-01a0669a-a7a5-71f1-8431-59a6fa3c6ce0"
        shard = (
            "rollout-2026-09-02T20-24-36-01a06201-c11e-7430-9dc1-57700eb22393_"
            "01a0624a-fb7e-7531-a77e-de8c2abc4417"
        )

        self.assertEqual(
            monitoring.display_rollout_id(root),
            "rollout-2026-09-03T16-30-06-01a0669a-a7a5-71f1-8431",
        )
        self.assertEqual(
            monitoring.display_rollout_id(shard),
            "rollout-2026-09-02T20-24-36-01a06201-c11e-7430-9dc1_"
            "01a0624a-fb7e-7531-a77e",
        )

    def test_overall_lines_are_shallow_and_mark_unmeasured_sections(self):
        empty_report = monitoring.OverallReport(
            total=monitoring.Usage(),
            projects=[],
            provider_usage={"claude": monitoring.Usage(), "codex": monitoring.Usage()},
            provider_sessions=monitoring.Counter(),
            session_count=0, discovered_count=0, prompt_count=0, unreadable=0, days=[],
        )
        lines = monitoring.overall_lines(empty_report, 100)
        self.assertLess(len(lines), 40)  # one shallow screen, not a drill-down view
        for section in monitoring.OVERALL_SECTIONS:
            self.assertIn(section, lines)
        joined = "\n".join(lines)
        self.assertIn("Not measured", joined)
        self.assertNotIn("$", joined)  # never invents pricing

    def test_o_opens_overall_and_p_exports_html_while_p_still_opens_processes_elsewhere(self):
        fixture = SessionFixture([prompt_with_cwd("/work/fixture"), assistant_usage()])

        class Screen:
            def getmaxyx(self):
                return 30, 100

        try:
            with mock.patch.object(monitoring, "find_all_session_entries", return_value=[(time.time(), fixture.path)]):
                app = monitoring.TTYApp(Screen(), fixture.path)
                app.refresh(force=True)

                app.handle_key(ord("p"))
                self.assertEqual(app.mode, "processes")
                app.handle_key(ord("p"))
                self.assertEqual(app.mode, "list")

                app.handle_key(ord("o"))
                self.assertEqual(app.mode, "overall")
                app.overall_thread.join(timeout=2)
                self.assertFalse(app.overall_thread.is_alive())
                self.assertIsNotNone(app.overall_report)

                with (
                    mock.patch.object(monitoring, "write_overall_report", return_value=(True, "saved /tmp/x.html")) as write,
                    mock.patch.object(monitoring, "open_overall_report", return_value=(True, "opened in browser · /tmp/x.html")) as open_report,
                ):
                    app.handle_key(ord("p"))
                    write.assert_called_once_with(app.overall_report)
                    open_report.assert_called_once_with()
                self.assertEqual(app.transient_status, "saved /tmp/x.html")
                self.assertIn("saved /tmp/x.html", app.frame()[-1][0])
                self.assertEqual(app.mode, "overall")

                app.handle_key(monitoring.curses.KEY_RIGHT)
                self.assertEqual(app.mode, "project")
                self.assertEqual(app.selected_project, "/work/fixture")
                project_page = "\n".join(row for row, _ in app.frame())
                self.assertIn("Model mix", project_page)
                self.assertIn("Top 5 sessions by consumption · last 30 days", project_page)
                self.assertIn("Top 5 files", project_page)
                app.handle_key(monitoring.curses.KEY_LEFT)
                self.assertEqual(app.mode, "overall")

                with (
                    mock.patch.object(monitoring, "write_overall_report", return_value=(True, "saved again")) as write,
                    mock.patch.object(monitoring, "open_overall_report", return_value=(True, "opened again")),
                ):
                    app.handle_key(ord("P"))
                    write.assert_called_once_with(app.overall_report)

                app.handle_key(monitoring.curses.KEY_LEFT)
                self.assertEqual(app.mode, "list")
        finally:
            fixture.close()

    def test_o_shows_loading_progress_without_blocking_the_tui(self):
        fixture = SessionFixture([prompt_with_cwd("/work/fixture"), assistant_usage()])
        started = threading.Event()
        release = threading.Event()

        class Screen:
            def getmaxyx(self):
                return 24, 100

        def slow_report(cache, window_days=monitoring.OVERALL_WINDOW_DAYS, progress=None):
            if progress:
                progress(7, 20)
            started.set()
            release.wait(timeout=2)
            return monitoring.OverallReport(
                total=monitoring.Usage(), projects=[],
                provider_usage={"claude": monitoring.Usage(), "codex": monitoring.Usage()},
                provider_sessions=monitoring.Counter(), session_count=0, discovered_count=0,
                prompt_count=0, unreadable=0, days=[],
            )

        try:
            app = monitoring.TTYApp(Screen(), fixture.path)
            with mock.patch.object(monitoring, "build_overall_report", side_effect=slow_report):
                app.handle_key(ord("o"))
                self.assertTrue(started.wait(timeout=1))
                self.assertEqual(app.overall_load_state, "loading")
                rendered = "\n".join(row for row, _ in app.frame())
                self.assertIn("Processing overall", app.frame()[-1][0])
                self.assertIn("7/20", rendered)
                self.assertNotIn("Reading Claude and Codex", rendered)
                app.handle_key(monitoring.curses.KEY_LEFT)
                self.assertEqual(app.mode, "list")
                self.assertIn("Processing overall", app.frame()[-1][0])
                release.set()
                app.overall_thread.join(timeout=2)
            self.assertEqual(app.overall_load_state, "ready")
        finally:
            release.set()
            fixture.close()

    def test_bottom_right_status_expires_after_ten_seconds(self):
        fixture = SessionFixture([prompt(), assistant_usage()])

        class Screen:
            def getmaxyx(self):
                return 24, 100

        try:
            app = monitoring.TTYApp(Screen(), fixture.path)
            with mock.patch.object(monitoring.time, "monotonic", return_value=100.0):
                app.set_status_area("saved /tmp/overall-report.html")
            with mock.patch.object(monitoring.time, "monotonic", return_value=109.9):
                self.assertEqual(app.status_area(), "saved /tmp/overall-report.html")
            with mock.patch.object(monitoring.time, "monotonic", return_value=110.0):
                self.assertEqual(app.status_area(), "")
        finally:
            fixture.close()

    def test_project_detail_ranks_models_recent_sessions_and_files_by_consumption(self):
        project = monitoring.ProjectUsage("lms", sessions=3, prompts=3)
        for label, model, tokens in (
            ("Implement enrollment", "claude-sonnet-5", 100),
            ("Fix report", "gpt-5.6-sol", 60),
            ("Review tests", "claude-sonnet-5", 20),
        ):
            usage = monitoring.Usage()
            usage.add(model, tokens - 5, 5, 0, 0)
            project.usage.merge(usage)
            project.recent_sessions.append(monitoring.SessionUsage(
                f"session-{model}", "", 1, label, "2026-09-03T10:00:00Z", "claude", usage,
                2, tokens // 2, tokens // 2 + 10, 80.0,
            ))
        project.files.update({"src/enrollment.py": 7, "tests/test_report.py": 3})

        lines = monitoring.project_detail_lines(project, 100)
        joined = "\n".join(lines)
        self.assertIn("sonnet-5", joined)
        self.assertIn("66.7%", joined)
        self.assertLess(joined.index("Implement enrollment"), joined.index("Fix report"))
        self.assertIn("src/enrollment.py · 7 operations", joined)
        self.assertIn("not consumption attribution", joined)
        self.assertIn("session session-claude-sonnet", joined)
        self.assertIn("ctx 50 latest / 60 peak · 80% cached overall", joined)

    def test_project_session_tokens_are_flush_right_with_thai_text(self):
        project = monitoring.ProjectUsage("lms", sessions=1, prompts=1)
        usage = monitoring.Usage()
        usage.add("opus-5", 300_000_000, 5_730_000, 0, 0)
        project.usage.merge(usage)
        project.recent_sessions.append(monitoring.SessionUsage(
            "72bdfdd0-6f98-485b-aee6-c8005ccc1fd7", "", 1,
            "ถ้าตอนนี้เราให้นายแก้ FE DS เพื่อ implement บน BO นายจะทำยังไงอะ",
            "2026-08-17T00:00:00Z", "claude", usage, 45, 272_200, 751_800, 100.0,
        ))

        task_line = next(
            line for line in monitoring.project_detail_lines(project, 200)
            if line.startswith("1. ")
        )

        self.assertTrue(task_line.endswith("305.73M"))
        self.assertEqual(
            monitoring.terminal_width(task_line), monitoring.PROJECT_DETAIL_CONTENT_WIDTH,
        )
        self.assertEqual(monitoring.truncate_terminal_layout(task_line, 120), task_line)

    def test_project_session_metadata_row_jumps_to_that_session(self):
        current = SessionFixture([prompt("Current"), assistant_usage()])
        target = SessionFixture([prompt("Target"), assistant_usage()])

        class Screen:
            def getmaxyx(self):
                return 24, 120

        try:
            app = monitoring.TTYApp(Screen(), current.path)
            usage = monitoring.create_analyzer(target.path).analysis.total_usage
            project = monitoring.ProjectUsage("lms", usage=usage, sessions=1, prompts=1, root="/work/lms")
            project.recent_sessions.append(monitoring.SessionUsage(
                "fixture", "", 1, "Target", "2026-09-03T00:00:00Z", "claude",
                usage, 1, 10, 10, 0.0, target.path,
            ))
            app.overall_report = monitoring.OverallReport(
                total=usage, projects=[project], provider_usage={"claude": usage},
                provider_sessions=monitoring.Counter(claude=1), session_count=1,
                discovered_count=1, prompt_count=1, unreadable=0, days=[],
            )
            app.selected_project = "/work/lms"
            app.mode = "project"

            rendered = app.frame()
            self.assertTrue(any("session fixture" in line and attr & monitoring.curses.A_REVERSE
                                for line, attr in rendered))
            with mock.patch.object(monitoring, "find_all_sessions", return_value=[target.path, current.path]):
                app.handle_key(monitoring.curses.KEY_ENTER)

            self.assertEqual(app.path, target.path)
            self.assertEqual(app.mode, "list")
            app.handle_key(monitoring.curses.KEY_LEFT)
            self.assertEqual(app.path, current.path)
            self.assertEqual(app.mode, "project")
        finally:
            current.close()
            target.close()

    def test_search_accepts_thai_and_hash_session_id_opens_detail(self):
        session_id = "72bdfdd0-6f98-485b-aee6-c8005ccc1fd7"
        record = prompt("แก้หน้ารายงานภาษาไทย")
        record["sessionId"] = session_id
        fixture = SessionFixture([record, assistant_usage()])

        class Screen:
            def getmaxyx(self):
                return 24, 120

        try:
            app = monitoring.TTYApp(Screen(), fixture.path)
            app.refresh(force=True)
            app.handle_key(ord("/"))
            for char in "ภาษาไทย":
                app.handle_key(char)
            self.assertEqual(app.search_query, "ภาษาไทย")
            app.handle_key("\n")
            self.assertEqual(app.mode, "list")
            self.assertTrue(app.search_matches)

            app.handle_key(ord("/"))
            for char in f"#{session_id}":
                app.handle_key(char)
            with mock.patch.object(monitoring, "find_all_sessions", return_value=[fixture.path]):
                app.handle_key("\n")

            self.assertEqual(app.path, fixture.path)
            self.assertEqual(app.mode, "detail")
            self.assertEqual(app.selected_prompt, 1)
            app.handle_key(27)
            self.assertEqual(app.path, fixture.path)
            self.assertEqual(app.mode, "list")
        finally:
            fixture.close()

    def test_render_overall_html_is_self_contained_and_escapes_untrusted_text(self):
        report = monitoring.OverallReport(
            total=monitoring.Usage(),
            projects=[monitoring.ProjectUsage(name="<script>alert(1)</script>", sessions=1, prompts=1)],
            provider_usage={"claude": monitoring.Usage(), "codex": monitoring.Usage()},
            provider_sessions=monitoring.Counter(claude=1),
            session_count=1, discovered_count=1, prompt_count=1, unreadable=0, days=[],
        )
        report.projects[0].usage.add("<img src=x onerror=alert(1)>", 10, 5, 0, 0)
        report.projects[0].recent_sessions.append(monitoring.SessionUsage(
            "<unsafe-id>", "", 1, "<b>unsafe session</b>", "2026-09-03T00:00:00Z", "claude",
            report.projects[0].usage, 1, 10, 15, 50.0,
        ))
        report.projects[0].files["<svg onload=alert(1)>"] = 2
        report.total.merge(report.projects[0].usage)
        report.provider_usage["claude"].merge(report.projects[0].usage)

        html_text = monitoring.render_overall_html(report, "2026-09-03 00:00:00")

        self.assertTrue(html_text.strip().startswith("<!doctype html>"))
        self.assertNotIn("<script>alert", html_text)
        self.assertNotIn("<img src=x", html_text)
        self.assertNotIn("<b>unsafe session", html_text)
        self.assertNotIn("<unsafe-id>", html_text)
        self.assertNotIn("<svg onload", html_text)
        self.assertIn("&lt;script&gt;", html_text)
        self.assertIn("<h2>Projects</h2>", html_text)
        self.assertEqual(html_text.count("<h2>Projects</h2>"), 1)
        self.assertNotIn("<h2>Top projects</h2>", html_text)
        self.assertIn("Top 5 sessions by consumption · last 30 days", html_text)
        self.assertIn("Consumption score", html_text)
        self.assertNotIn("Total tokens", html_text)
        self.assertIn("Fresh ×1", html_text)
        self.assertIn("Cache ×0.1", html_text)
        self.assertIn('class="session-main"', html_text)
        self.assertIn('class="session-meta"', html_text)
        self.assertNotIn("<th>Session / rollout ID</th>", html_text)
        self.assertIn("latest", html_text)
        self.assertIn("peak", html_text)
        self.assertIn(".bar-fill { display: block;", html_text)
        self.assertNotIn("http://", html_text)
        self.assertNotIn("https://", html_text)
        self.assertNotIn("src=\"", html_text)
        self.assertNotIn("href=", html_text)

    def test_write_overall_report_reports_saved_path_and_surfaces_failures(self):
        report = monitoring.OverallReport(
            total=monitoring.Usage(),
            projects=[],
            provider_usage={"claude": monitoring.Usage(), "codex": monitoring.Usage()},
            provider_sessions=monitoring.Counter(),
            session_count=0, discovered_count=0, prompt_count=0, unreadable=0, days=[],
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "nested" / "overall-report.html"
            with mock.patch.object(monitoring, "default_overall_report_path", return_value=target):
                ok, message = monitoring.write_overall_report(report)
            self.assertTrue(ok)
            self.assertIn(str(target), message)
            self.assertTrue(target.is_file())
            self.assertIn("<!doctype html>", target.read_text())

        with (
            mock.patch.object(monitoring, "default_overall_report_path", return_value=Path("/no/such/dir/report.html")),
            mock.patch.object(Path, "mkdir", side_effect=OSError("denied")),
        ):
            ok, message = monitoring.write_overall_report(report)
        self.assertFalse(ok)
        self.assertTrue(message.startswith("export failed"))


if __name__ == "__main__":
    unittest.main()


def subagent_record(agent_id, prompt_id, **usage_kwargs):
    record = assistant_usage(**usage_kwargs)
    record.update({"isSidechain": True, "agentId": agent_id, "promptId": prompt_id})
    return record


class SubagentAccountingTests(unittest.TestCase):
    def write_session(self, root, agent_records, meta=None):
        session = Path(root) / "session-1.jsonl"
        main_prompt = prompt()
        main_prompt["promptId"] = "prompt-1"
        session.write_text(
            json.dumps(main_prompt) + "\n" + json.dumps(assistant_usage(inp=10, out=5)) + "\n",
            encoding="utf-8",
        )
        agent_dir = Path(root) / "session-1" / "subagents"
        agent_dir.mkdir(parents=True)
        transcript = agent_dir / "agent-abc.jsonl"
        transcript.write_text("".join(json.dumps(item) + "\n" for item in agent_records), encoding="utf-8")
        if meta is not None:
            (agent_dir / "agent-abc.meta.json").write_text(json.dumps(meta), encoding="utf-8")
        return session, transcript

    def test_subagent_transcripts_are_attributed_to_their_prompt(self):
        with tempfile.TemporaryDirectory() as root:
            first = {
                "type": "user", "isSidechain": True, "promptId": "prompt-1", "agentId": "abc",
                "timestamp": "2026-07-20T10:00:02Z", "message": {"role": "user", "content": "Review"},
            }
            usage = subagent_record("abc", "prompt-1", inp=3, out=7, cache_read=100, timestamp="2026-07-20T10:00:03Z")
            usage["requestId"] = "req-sub-1"
            session, transcript = self.write_session(
                root, [first, usage], meta={"agentType": "general-purpose", "description": "Review data flow"},
            )
            analyzer = monitoring.IncrementalSessionAnalyzer(str(session))
            turn = analyzer.analysis.prompts[0]
            self.assertEqual(turn.prompt_id, "prompt-1")
            self.assertEqual(turn.main.total, 15)
            self.assertEqual(list(turn.sub_sessions), ["agentId:abc"])
            sub = turn.sub_sessions["agentId:abc"]
            self.assertEqual(sub.label, "Review data flow · general-purpose")
            self.assertEqual(sub.usage.total, 110)
            self.assertEqual(turn.total_usage.total, 125)
            self.assertEqual(analyzer.analysis.total_usage.total, 125)

            # A streamed duplicate is merged once; a fresh request is appended live.
            with open(transcript, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(usage) + "\n")
                more = subagent_record("abc", "prompt-1", inp=1, out=1, timestamp="2026-07-20T10:00:04Z")
                more["requestId"] = "req-sub-2"
                fh.write(json.dumps(more) + "\n")
            self.assertTrue(analyzer.poll())
            self.assertEqual(sub.usage.requests, 2)
            self.assertEqual(sub.usage.total, 112)
            self.assertFalse(analyzer.poll())

    def test_subagent_without_prompt_id_falls_back_to_timestamp(self):
        with tempfile.TemporaryDirectory() as root:
            usage = subagent_record("abc", None, inp=2, out=2, timestamp="2026-07-20T10:00:03Z")
            del usage["promptId"]
            session, _ = self.write_session(root, [usage])
            turn = monitoring.IncrementalSessionAnalyzer(str(session)).analysis.prompts[0]
            self.assertEqual(turn.sub_sessions["agentId:abc"].label, "fable sub-session")
            self.assertEqual(turn.total_usage.total, 19)

    def test_sqlite_cache_resumes_subagent_offsets(self):
        cache_file = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        cache_file.close()
        try:
            with tempfile.TemporaryDirectory() as root:
                usage = subagent_record("abc", "prompt-1", inp=3, out=7, timestamp="2026-07-20T10:00:03Z")
                usage["requestId"] = "req-sub-1"
                session, transcript = self.write_session(root, [usage])
                cache = monitoring.ProfilerCache(cache_file.name)
                first = cache.analyzer(str(session))
                self.assertEqual(first.analysis.total_usage.total, 25)
                offset = first.subagents[str(transcript)]["offset"]
                self.assertEqual(offset, os.path.getsize(transcript))

                more = subagent_record("abc", "prompt-1", inp=1, out=1, timestamp="2026-07-20T10:00:04Z")
                more["requestId"] = "req-sub-2"
                with open(transcript, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(more) + "\n")
                resumed = cache.analyzer(str(session))
                self.assertGreater(resumed.subagents[str(transcript)]["offset"], offset)
                self.assertEqual(resumed.analysis.total_usage.total, 27)
                self.assertEqual(resumed.analysis.prompts[0].sub_sessions["agentId:abc"].usage.requests, 2)
                cache.db.close()
        finally:
            os.unlink(cache_file.name)


class CodexTokenCountDedupTests(unittest.TestCase):
    @staticmethod
    def token_count(timestamp, last, total):
        return {"type": "event_msg", "timestamp": timestamp, "payload": {
            "type": "token_count", "info": {"last_token_usage": last, "total_token_usage": total},
        }}

    def write_rollout(self, records):
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", prefix="rollout-", delete=False, encoding="utf-8",
        )
        for record in records:
            handle.write(json.dumps(record) + "\n")
        handle.close()
        return handle.name

    def test_replayed_token_count_snapshots_are_counted_once(self):
        first_last = {"input_tokens": 100, "cached_input_tokens": 70, "output_tokens": 5, "total_tokens": 105}
        first_total = dict(first_last)
        second_last = {"input_tokens": 40, "cached_input_tokens": 30, "output_tokens": 3, "total_tokens": 43}
        second_total = {"input_tokens": 140, "cached_input_tokens": 100, "output_tokens": 8, "total_tokens": 148}
        records = [item for item in codex_records() if item["payload"].get("type") != "token_count"]
        records[-1:-1] = [
            self.token_count("2026-07-20T10:00:04Z", first_last, first_total),
            # Codex re-emits the identical snapshot when only rate limits refresh.
            self.token_count("2026-07-20T10:00:05Z", first_last, first_total),
            self.token_count("2026-07-20T10:00:06Z", second_last, second_total),
        ]
        path = self.write_rollout(records)
        try:
            turn = monitoring.create_analyzer(path).analysis.prompts[0]
            self.assertEqual(turn.main.requests, 2)
            self.assertEqual(len(turn.requests), 2)
            self.assertEqual(turn.main.context_total, 140)
            self.assertEqual(turn.main.cache_read, 100)
            self.assertEqual(turn.main.output, 8)
        finally:
            os.unlink(path)

    def test_dedup_baseline_survives_cache_resume(self):
        last = {"input_tokens": 100, "cached_input_tokens": 70, "output_tokens": 5, "total_tokens": 105}
        records = [item for item in codex_records() if item["payload"].get("type") != "token_count"]
        records[-1:-1] = [self.token_count("2026-07-20T10:00:04Z", last, dict(last))]
        path = self.write_rollout(records)
        cache_file = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        cache_file.close()
        try:
            cache = monitoring.ProfilerCache(cache_file.name)
            cache.analyzer(path)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(self.token_count("2026-07-20T10:00:07Z", last, dict(last))) + "\n")
            resumed = cache.analyzer(path)
            self.assertEqual(resumed.analysis.prompts[0].main.requests, 1)
            cache.db.close()
        finally:
            os.unlink(path)
            os.unlink(cache_file.name)


def small_overall_report(session_count=1):
    usage = monitoring.Usage()
    usage.add("claude-fable", 10, 5, 0, 100)
    project = monitoring.ProjectUsage("tools", usage=usage, sessions=session_count, prompts=1, root="/work/tools")
    return monitoring.OverallReport(
        total=usage, projects=[project], provider_usage={"claude": usage},
        provider_sessions=monitoring.Counter(claude=session_count), session_count=session_count,
        discovered_count=session_count, prompt_count=1, unreadable=0, days=[],
    )


class DetachedDashboardTests(unittest.TestCase):
    def test_exported_report_stays_script_free(self):
        exported = monitoring.render_overall_html(small_overall_report(), "now")
        self.assertNotIn("<script", exported)
        self.assertNotIn("https://", exported)

    def test_report_payload_carries_per_day_providers_models_and_every_session(self):
        report = small_overall_report(session_count=2)
        day = monitoring.DayUsage("2026-07-20")
        day.usage.add("claude-fable", 10, 5, 0, 100)
        day.providers["claude"] = monitoring.Usage()
        day.providers["claude"].add("claude-fable", 10, 5, 0, 100)
        report.days.append(day)
        report.sessions.append(monitoring.SessionUsage(
            "sess-1", "", 1, "Run review", "2026-07-20T10:00:00+00:00", "claude", report.total,
            1, 110, 110, 90.9, "/tmp/sess-1.jsonl", "/work/tools",
        ))
        payload = monitoring.report_payload(report, "now", 4)
        self.assertEqual(payload["version"], 4)
        self.assertEqual(payload["weights"], {"fresh": 1.0, "cache": 0.1})
        self.assertEqual(payload["days"][0]["providers"]["claude"]["cache_read"], 100)
        self.assertEqual(payload["days"][0]["usage"]["models"]["claude-fable"]["cache"], 100)
        self.assertEqual(payload["sessions"][0]["id"], "sess-1")
        self.assertEqual(payload["sessions"][0]["project"], "tools")
        self.assertEqual(payload["total"]["consumption"], 15 + 10)
        self.assertEqual(json.loads(json.dumps(payload))["scope"]["sessions"], 2)

    def test_dashboard_serves_report_session_and_prompt_detail(self):
        import urllib.error
        import urllib.request

        first = prompt()
        first["sessionId"] = "abcd1234-0000-0000-0000-000000000001"
        fixture = SessionFixture([first, request_with_actor()])
        html_file = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8")
        html_file.write("<!doctype html><title>test page</title><p>spa</p>")
        html_file.close()
        rebuilt = small_overall_report(session_count=7)
        rebuilt.sessions.append(monitoring.SessionUsage(
            first["sessionId"], "", 1, "Run review", "2026-07-20T10:00:00+00:00", "claude",
            rebuilt.total, 1, 0, 0, 0.0, fixture.path, "/work/tools",
        ))
        server = monitoring.DashboardServer(
            None, initial=small_overall_report(), refresh_seconds=3600,
            build=lambda cache: rebuilt, html_path=Path(html_file.name),
        )
        url = server.start()
        try:
            self.assertTrue(url.startswith("http://127.0.0.1:"))
            with urllib.request.urlopen(url) as response:
                self.assertIn("test page", response.read().decode("utf-8"))
                self.assertEqual(response.headers["Cache-Control"], "no-store")
            with urllib.request.urlopen(url + "api/report") as response:
                self.assertEqual(json.loads(response.read())["version"], 1)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(url + "api/session/" + first["sessionId"])
            self.assertEqual(caught.exception.code, 404)

            self.assertTrue(server.rebuild())
            with urllib.request.urlopen(url + "api/status") as response:
                status = json.loads(response.read())
            self.assertEqual((status["version"], status["sessions"]), (2, 7))
            with urllib.request.urlopen(url + "api/session/" + first["sessionId"]) as response:
                session = json.loads(response.read())
            self.assertEqual(session["provider"], "claude")
            self.assertEqual(len(session["prompts"]), 1)
            self.assertEqual(session["prompts"][0]["prompt"], "Run review")
            self.assertEqual(session["prompts"][0]["requests"], 1)
            with urllib.request.urlopen(url + "api/session/" + first["sessionId"] + "/prompt/1") as response:
                detail = json.loads(response.read())
            self.assertEqual(detail["position"], 1)
            self.assertEqual(len(detail["requests"]), 1)
            self.assertEqual(len(detail["actors"]), 1)
            self.assertTrue(detail["timeline"])
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(url + "api/session/" + first["sessionId"] + "/prompt/9")
            self.assertEqual(caught.exception.code, 404)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(url + "nope")
            self.assertEqual(caught.exception.code, 404)
        finally:
            server.stop()
            fixture.close()
            os.unlink(html_file.name)
        self.assertFalse(server.running)
        with self.assertRaises(urllib.error.URLError):
            urllib.request.urlopen(url, timeout=1)

    def test_failed_rebuild_keeps_previous_report_and_reports_error(self):
        def explode(cache):
            raise RuntimeError("disk on fire")

        server = monitoring.DashboardServer(None, initial=small_overall_report(), refresh_seconds=3600, build=explode)
        self.assertFalse(server.rebuild())
        status = server.status()
        self.assertEqual(status["version"], 1)
        self.assertEqual(status["error"], "disk on fire")
        self.assertEqual(server.report["scope"]["sessions"], 1)

    def test_d_detaches_current_view_from_any_mode_and_D_stops(self):
        first = prompt()
        first["sessionId"] = "abcd1234-0000-0000-0000-000000000002"
        fixture = SessionFixture([first, assistant_usage()])

        class Screen:
            def getmaxyx(self):
                return 24, 120

        try:
            with mock.patch.dict(os.environ, {"AGENT_MONITOR_NO_BROWSER": "1"}):
                app = monitoring.TTYApp(Screen(), fixture.path)
                app.refresh(force=True)
                app.handle_key(ord("D"))
                self.assertIn("No dashboard running", app.transient_status)

                app.handle_key(ord("d"))
                self.assertIsNotNone(app.dashboard)
                self.assertTrue(app.dashboard.running)
                self.assertIn("#/session/" + first["sessionId"], app.transient_status)
                self.assertIn("browser opening disabled", app.transient_status)

                app.mode = "detail"
                app.selected_prompt = 1
                app.detail_page = "request:0"
                self.assertEqual(app.detach_route(), f"#/session/{first['sessionId']}/prompt/1/request/1")
                app.mode = "overall"
                self.assertEqual(app.detach_route(), "#/overview")
                app.mode = "project"
                self.assertEqual(app.detach_route(), "#/projects")
                server = app.dashboard
                app.handle_key(ord("d"))
                self.assertIs(app.dashboard, server)
                self.assertIn("#/projects", app.transient_status)

                app.handle_key(ord("D"))
                self.assertIsNone(app.dashboard)
                self.assertFalse(server.running)
                self.assertEqual(app.transient_status, "Dashboard stopped")
        finally:
            fixture.close()
