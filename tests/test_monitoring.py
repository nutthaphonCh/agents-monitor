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
            "type": "user_message", "message": "Fix the profiler",
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

    def test_brackets_navigate_newest_first_chats_chronologically(self):
        app = object.__new__(monitoring.TTYApp)
        app.mode = "list"
        movements = []
        app.switch_session = movements.append

        app.handle_key(ord("["))
        app.handle_key(ord("]"))

        self.assertEqual(movements, [1, -1])


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
            cache.db.close()
        finally:
            if not session.closed:
                session.close()
            os.unlink(session.name)
            os.unlink(cache_file.name)


class FrameAffordanceTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
