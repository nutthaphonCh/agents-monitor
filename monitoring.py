#!/usr/bin/env python3
"""
monitoring.py — live execution profiler for Claude Code and Codex sessions.

Views
  [1] Live  prompt-centric execution tree, auto-refreshed
  [2] History  newest prompts first

Keys
  1 / 2       switch view
  [           previous session
  ]           next session
  z / x       Claude / Codex sessions
  p           background processes for the current session
  Up / Down   move between prompts
  Right/Enter open; Left/Esc back
  PgUp/PgDn   scroll one page
  Home / End  first prompt / follow latest
  /, n / N    search, next / previous result
  ?           help
  r           force refresh
  q           quit

Usage
  python3 monitoring.py
  python3 monitoring.py <session.jsonl>
  python3 monitoring.py <project-dir>
  python3 monitoring.py --list

No dependencies — Python 3 standard library only.
"""

from __future__ import annotations

import argparse
import curses
import datetime as dt
import glob
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import textwrap
import time

from collections import Counter
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any


REFRESH_INTERVAL = 0.5
CACHE_SCHEMA_VERSION = 10
ESCAPE_DELAY_MS = 25
ACTION_NAMES = (
    "WebSearch", "WebFetch", "Bash", "Write", "Edit", "Read", "Glob", "Grep", "Search",
)
ACTION_PALETTES = {
    "bash": (1, curses.COLOR_BLACK, curses.COLOR_YELLOW),
    "write": (2, curses.COLOR_BLACK, curses.COLOR_GREEN),
    "edit": (3, curses.COLOR_BLACK, curses.COLOR_CYAN),
    "read": (4, curses.COLOR_WHITE, curses.COLOR_BLUE),
    "glob": (5, curses.COLOR_WHITE, curses.COLOR_MAGENTA),
    "grep": (5, curses.COLOR_WHITE, curses.COLOR_MAGENTA),
    "search": (5, curses.COLOR_WHITE, curses.COLOR_MAGENTA),
    "websearch": (6, curses.COLOR_BLACK, curses.COLOR_RED),
    "webfetch": (7, curses.COLOR_BLACK, curses.COLOR_WHITE),
}


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_create: int = 0
    cache_read: int = 0
    requests: int = 0
    models: Counter[str] = field(default_factory=Counter)

    def add(self, model: str, inp: int, out: int, cache_create: int, cache_read: int) -> None:
        self.input += inp
        self.output += out
        self.cache_create += cache_create
        self.cache_read += cache_read
        self.requests += 1
        self.models[model] += 1

    def merge(self, other: "Usage") -> None:
        self.input += other.input
        self.output += other.output
        self.cache_create += other.cache_create
        self.cache_read += other.cache_read
        self.requests += other.requests
        self.models.update(other.models)

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_create + self.cache_read

    @property
    def context_total(self) -> int:
        return self.input + self.cache_create + self.cache_read

    @property
    def cache_hit_rate(self) -> float:
        return (self.cache_read / self.context_total * 100) if self.context_total else 0.0

    @property
    def primary_model(self) -> str:
        return self.models.most_common(1)[0][0] if self.models else "unknown"


@dataclass
class SubSession:
    key: str
    label: str
    first_sequence: int
    usage: Usage = field(default_factory=Usage)


@dataclass
class Actor:
    key: str
    label: str
    status: str = "running"
    started_at: str | None = None
    finished_at: str | None = None
    task_id: str | None = None
    exit_code: int | None = None
    tool_use_id: str | None = None
    telemetry_path: str | None = None
    working_dir: str | None = None
    thread_id: str | None = None
    duration_seconds: float | None = None
    token_usage: dict[str, int] = field(default_factory=dict)
    rate_limits: dict[str, Any] | None = None
    engine: str | None = None
    model: str | None = None
    mode: str | None = None
    lane: str | None = None
    verdict: str | None = None
    decision: str | None = None
    rationale: str | None = None
    output_path: str | None = None
    events_path: str | None = None
    log_chunks: list[str] = field(default_factory=list)


@dataclass
class TimelineItem:
    timestamp: str | None
    label: str
    kind: str


@dataclass
class FileActivity:
    path: str
    action: str
    timestamp: str | None


@dataclass
class RequestInfo:
    timestamp: str | None
    model: str
    usage: Usage
    request_id: str = ""
    stop_reason: str | None = None
    actions: list[str] = field(default_factory=list)
    action_details: list[str] = field(default_factory=list)
    action_outputs: list[str] = field(default_factory=list)
    last_timestamp: str | None = None


@dataclass
class UsageObservation:
    before_time: str
    after_time: str
    before_5h: float
    after_5h: float
    before_weekly: float
    after_weekly: float
    confidence: str
    confidence_reason: str

    @property
    def delta_5h(self) -> float:
        return self.after_5h - self.before_5h

    @property
    def delta_weekly(self) -> float:
        return self.after_weekly - self.before_weekly


@dataclass
class PromptTurn:
    index: int
    prompt: str
    start_sequence: int
    timestamp: str | None
    main: Usage = field(default_factory=Usage)
    sub_sessions: dict[str, SubSession] = field(default_factory=dict)
    events: list[str] = field(default_factory=list)
    actors: list[Actor] = field(default_factory=list)
    timeline: list[TimelineItem] = field(default_factory=list)
    files: list[FileActivity] = field(default_factory=list)
    requests: list[RequestInfo] = field(default_factory=list)
    evidence_chars: Counter[str] = field(default_factory=Counter)
    usage_observations: list[UsageObservation] = field(default_factory=list)
    accounted_usage_keys: set[str] = field(default_factory=set, repr=False)
    request_info_keys: set[str] = field(default_factory=set, repr=False)

    @property
    def total_usage(self) -> Usage:
        total = Usage()
        total.merge(self.main)
        for sub in self.sub_sessions.values():
            total.merge(sub.usage)
        return total


@dataclass
class Analysis:
    path: str
    prompts: list[PromptTurn]
    preamble: Usage
    malformed: int
    record_count: int
    provider: str = "claude"

    @property
    def total_usage(self) -> Usage:
        total = Usage()
        total.merge(self.preamble)
        for prompt in self.prompts:
            total.merge(prompt.total_usage)
        return total


def default_projects_dir() -> Path:
    return Path(os.path.expanduser("~/.claude/projects"))


def default_codex_sessions_dir() -> Path:
    return Path(os.path.expanduser("~/.codex/sessions"))


def default_codex_state_db() -> Path:
    return Path(os.path.expanduser("~/.codex/state_5.sqlite"))


def codex_session_metadata(path: str) -> tuple[str, str] | None:
    """Return (chat id, thread source) from a rollout's session metadata."""
    try:
        with Path(path).open(encoding="utf-8") as session_file:
            record = json.loads(session_file.readline())
    except (OSError, json.JSONDecodeError):
        return None
    if record.get("type") != "session_meta":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    chat_id = payload.get("session_id") or payload.get("id")
    if not isinstance(chat_id, str) or not chat_id:
        return None
    source = payload.get("thread_source")
    return chat_id, source if isinstance(source, str) else ""


def fallback_codex_chats() -> list[tuple[float, str]]:
    """Discover one latest user-facing rollout per chat without the state DB."""
    root = default_codex_sessions_dir()
    if not root.exists():
        return []
    chats: dict[str, tuple[float, str]] = {}
    for path in glob.glob(str(root / "**" / "*.jsonl"), recursive=True):
        metadata = codex_session_metadata(path)
        if metadata is None:
            chat_id, source = path, ""
        else:
            chat_id, source = metadata
        if source == "subagent":
            continue
        modified = os.path.getmtime(path)
        current = chats.get(chat_id)
        if current is None or modified > current[0]:
            chats[chat_id] = (modified, path)
    return list(chats.values())


def codex_chats() -> list[tuple[float, str]]:
    """Read Codex's chat ordering and current rollout path from local state."""
    state_db = default_codex_state_db()
    try:
        connection = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                """
                SELECT rollout_path, recency_at_ms, recency_at,
                       updated_at_ms, updated_at, created_at_ms, created_at
                FROM threads
                WHERE archived = 0
                  AND (thread_source = 'user' OR has_user_event = 1)
                """
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return fallback_codex_chats()

    chats: list[tuple[float, str]] = []
    seen: set[str] = set()
    for path, recency_ms, recency, updated_ms, updated, created_ms, created in rows:
        if not isinstance(path, str) or path in seen or not Path(path).is_file():
            continue
        seen.add(path)
        timestamp = next(
            (
                value / 1000 if is_milliseconds else value
                for value, is_milliseconds in (
                    (recency_ms, True), (recency, False),
                    (updated_ms, True), (updated, False),
                    (created_ms, True), (created, False),
                )
                if isinstance(value, (int, float)) and value > 0
            ),
            os.path.getmtime(path),
        )
        chats.append((timestamp, path))
    return chats or fallback_codex_chats()


def find_all_session_entries() -> list[tuple[float, str]]:
    sessions: list[tuple[float, str]] = []
    claude_root = default_projects_dir()
    if claude_root.exists():
        for path in glob.glob(str(claude_root / "**" / "*.jsonl"), recursive=True):
            if "subagents" not in Path(path).parts:
                sessions.append((os.path.getmtime(path), path))
    sessions.extend(codex_chats())
    return sorted(sessions, key=lambda item: (item[0], item[1]), reverse=True)


def find_all_sessions() -> list[str]:
    return [path for _, path in find_all_session_entries()]


def session_provider(path: str) -> str:
    if Path(path).name.startswith("rollout-") or default_codex_sessions_dir() in Path(path).parents:
        return "codex"
    return "claude"


def resolve_target(arg: str | None) -> str:
    if arg is None:
        sessions = find_all_sessions()
        if not sessions:
            raise SystemExit(f"No session .jsonl files under {default_projects_dir()}")
        return sessions[0]

    path = Path(os.path.expanduser(arg))
    if path.is_dir():
        files = sorted(glob.glob(str(path / "**" / "*.jsonl"), recursive=True), key=os.path.getmtime, reverse=True)
        if not files:
            raise SystemExit(f"No .jsonl files in {path}")
        return files[0]
    if path.is_file():
        return str(path)
    raise SystemExit(f"Not found: {arg}")


def normalize_text(value: str) -> str:
    return " ".join((value or "").split())


def local_token_weight(value: str) -> int:
    """Tokenizer-free evidence weight; totals are later reconciled to provider telemetry."""
    if not value:
        return 0
    ascii_chars = sum(1 for char in value if ord(char) < 128)
    non_ascii_chars = len(value) - ascii_chars
    return max(1, round(ascii_chars / 4 + non_ascii_chars))


def truncate(value: str, limit: int) -> str:
    value = normalize_text(value)
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


def truncate_layout(value: str, limit: int) -> str:
    """Truncate rendered TUI content without collapsing alignment whitespace."""
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


def short_model(model: str, limit: int = 18) -> str:
    value = (model or "unknown").split("/")[-1]
    if value.startswith("claude-"):
        value = value[len("claude-"):]
    return truncate(value, limit)


def fmt_tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def message_content(rec: dict[str, Any]) -> Any:
    msg = rec.get("message")
    return msg.get("content") if isinstance(msg, dict) else None


def text_blocks(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [
        block.get("text", "")
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]


def content_text(content: Any) -> str:
    return normalize_text(" ".join(text_blocks(content)))


def has_tool_result(content: Any) -> bool:
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content
    )


def is_synthetic(rec: dict[str, Any]) -> bool:
    if rec.get("isSynthetic") is True:
        return True
    subtype = str(rec.get("subtype") or rec.get("eventType") or "").lower()
    if subtype in {"synthetic", "task_notification", "task-notification", "interrupt", "interrupted"}:
        return True
    text = content_text(message_content(rec)).lower()
    return any(marker in text for marker in (
        "[request interrupted by user]",
        "<task-notification>",
        "<task_notification>",
    ))


def parse_usage_observation(text: str) -> UsageObservation | None:
    normalized = normalize_text(text)
    if not normalized.lower().startswith("usage observation"):
        return None
    match = re.search(
        r"Before\s+(\d{1,2}:\d{2}:\d{2})\s+5h\s+([\d.]+)%\s+weekly\s+([\d.]+)%\s+"
        r"After\s+(\d{1,2}:\d{2}:\d{2})\s+5h\s+([\d.]+)%\s+weekly\s+([\d.]+)%.*?"
        r"Confidence\s+([^·]+?)(?:\s*·\s*(.*))?$",
        normalized,
        re.I,
    )
    if not match:
        return None
    return UsageObservation(
        before_time=match.group(1), before_5h=float(match.group(2)), before_weekly=float(match.group(3)),
        after_time=match.group(4), after_5h=float(match.group(5)), after_weekly=float(match.group(6)),
        confidence=match.group(7).strip(), confidence_reason=(match.group(8) or "").strip(),
    )


def local_command_payload(text: str, tag: str) -> str | None:
    match = re.search(rf"<{re.escape(tag)}>(.*?)</{re.escape(tag)}>", text, re.I | re.S)
    return normalize_text(match.group(1)) if match else None


def is_real_user_prompt(rec: dict[str, Any]) -> bool:
    if rec.get("type") != "user" or rec.get("isSidechain") or is_synthetic(rec):
        return False
    content = message_content(rec)
    text = content_text(content)
    if local_command_payload(text, "local-command-stdout") is not None:
        return False
    if "<local-command-caveat>" in text.lower():
        return False
    if text.startswith("Base directory for this skill:") or parse_usage_observation(text):
        return False
    if rec.get("isCompactSummary"):
        return False
    if isinstance(content, str):
        return bool(content.strip())
    return bool(text_blocks(content)) and not has_tool_result(content)


def prompt_text(rec: dict[str, Any]) -> str:
    text = content_text(message_content(rec))
    command_match = re.search(r"<command-name>\s*([^<]+?)\s*</command-name>", text, re.I)
    if command_match:
        command = command_match.group(1).strip()
        command = command if command.startswith("/") else f"/{command}"
        args_match = re.search(r"<command-args>\s*(.*?)\s*</command-args>", text, re.I)
        args = args_match.group(1).strip() if args_match else ""
        return truncate(f"{command} {args}".strip(), 120)
    return truncate(text or "(empty prompt)", 120)


def extract_usage(rec: dict[str, Any]) -> tuple[str, int, int, int, int] | None:
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return None
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return None
    return (
        str(msg.get("model") or rec.get("model") or "unknown"),
        int(usage.get("input_tokens", 0) or 0),
        int(usage.get("output_tokens", 0) or 0),
        int(usage.get("cache_creation_input_tokens", 0) or 0),
        int(usage.get("cache_read_input_tokens", 0) or 0),
    )


def usage_identity(rec: dict[str, Any]) -> str:
    msg = rec.get("message")
    message_id = msg.get("id") if isinstance(msg, dict) else None
    return str(rec.get("requestId") or rec.get("request_id") or message_id or rec.get("uuid") or rec.get("id") or "")


def explicit_sub_key(rec: dict[str, Any]) -> str | None:
    for key in (
        "agentId", "agent_id", "sidechainId", "sidechain_id",
        "subagentId", "subagent_id", "taskId", "task_id",
    ):
        value = rec.get(key)
        if value:
            return f"{key}:{value}"
    metadata = rec.get("metadata")
    if isinstance(metadata, dict):
        for key in ("agentId", "agent_id", "sidechainId", "taskId"):
            value = metadata.get(key)
            if value:
                return f"{key}:{value}"
    return None


def sub_session_key(rec: dict[str, Any], prompt_index: int, sequence: int) -> str:
    explicit = explicit_sub_key(rec)
    if explicit:
        return explicit
    parent = rec.get("parentUuid") or rec.get("parent_uuid")
    if parent:
        return f"branch:{parent}"
    uuid = rec.get("uuid") or rec.get("id")
    if uuid:
        return f"branch:{uuid}"
    return f"sidechain:{prompt_index}:{sequence}"


def event_label(rec: dict[str, Any]) -> str | None:
    text = content_text(message_content(rec))
    lowered = text.lower()

    stdout = local_command_payload(text, "local-command-stdout")
    if stdout is not None:
        return truncate(f"Local command · {stdout}", 120)
    if "<local-command-caveat>" in lowered:
        return None
    if not is_synthetic(rec):
        return None

    if "[request interrupted by user]" in lowered:
        return "Interrupted by user"

    if "<task-notification>" in lowered or "<task_notification>" in lowered:
        task_match = re.search(r"<task[-_]id>\s*([^<]+?)\s*</task[-_]id>", text, re.I)
        status_match = re.search(r"<status>\s*([^<]+?)\s*</status>", text, re.I)
        summary_match = re.search(r"<summary>\s*([^<]+?)\s*</summary>", text, re.I)
        task_id = task_match.group(1).strip() if task_match else "unknown"
        status = status_match.group(1).strip() if status_match else "updated"
        label = f"Subtask {task_id}"
        if summary_match:
            summary = summary_match.group(1).strip()
            summary = re.sub(r'^Background command\s+["“](.*)["”]\s+', r"\1 · ", summary)
            label += f" · {summary}"
        else:
            label += f" · {status}"
        return truncate(label, 120)

    return truncate(text or str(rec.get("subtype") or "Synthetic event"), 120)


def analyze(path: str) -> Analysis:
    prompts: list[PromptTurn] = []
    current: PromptTurn | None = None
    preamble = Usage()
    preamble_usage_keys: set[str] = set()
    malformed = 0
    record_count = 0

    with open(path, "r", encoding="utf-8") as fh:
        for sequence, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if not isinstance(rec, dict):
                continue

            record_count += 1

            if is_real_user_prompt(rec):
                current = PromptTurn(
                    index=len(prompts) + 1,
                    prompt=prompt_text(rec),
                    start_sequence=sequence,
                    timestamp=rec.get("timestamp"),
                )
                prompts.append(current)
                continue

            label = event_label(rec)
            if label:
                if current:
                    current.events.append(label)
                continue

            usage_tuple = extract_usage(rec)
            if not usage_tuple:
                continue

            usage_key = usage_identity(rec)
            seen_keys = current.accounted_usage_keys if current else preamble_usage_keys
            if usage_key and usage_key in seen_keys:
                continue
            if usage_key:
                seen_keys.add(usage_key)

            model, inp, out, cache_create, cache_read = usage_tuple
            usage = Usage()
            usage.add(model, inp, out, cache_create, cache_read)

            if current is None:
                preamble.merge(usage)
                continue

            if rec.get("isSidechain"):
                key = sub_session_key(rec, current.index, sequence)
                sub = current.sub_sessions.get(key)
                if not sub:
                    sub = SubSession(
                        key=key,
                        label=f"{short_model(model)} sub-session",
                        first_sequence=sequence,
                    )
                    current.sub_sessions[key] = sub
                sub.usage.merge(usage)
            else:
                current.main.merge(usage)

    return Analysis(
        path=path,
        prompts=prompts,
        preamble=preamble,
        malformed=malformed,
        record_count=record_count,
    )


FILE_TOOL_ACTIONS = {
    "Read": "read", "Write": "write", "Edit": "edit",
    "Glob": "search", "Grep": "search",
}

# Claude Code injects these into the model context as their own record types, so they
# carry real context weight even though they never appear as user/assistant messages.
ATTACHMENT_EVIDENCE = {
    "skill_listing": "skills",
    "dynamic_skill": "skills",
    "edited_text_file": "repository",
}

AGENT_INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")


def content_blocks(content: Any, block_type: str) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict) and block.get("type") == block_type]


def actor_for_tool(prompt: PromptTurn, tool_use_id: str) -> Actor | None:
    return next((actor for actor in prompt.actors if actor.tool_use_id == tool_use_id), None)


def tool_activity_label(name: str, inputs: dict[str, Any]) -> str:
    if name in {"Read", "Write", "Edit"}:
        target = str(inputs.get("file_path") or inputs.get("path") or "file")
        return f"{name} · {target}"
    if name in {"Glob", "Grep"}:
        target = str(inputs.get("pattern") or inputs.get("query") or "repository")
        return f"{name} · {target}"
    if name == "Bash":
        purpose = str(inputs.get("description") or inputs.get("command") or "command")
        return f"Bash · {truncate(purpose, 72)}"
    if name == "Skill":
        return f"Skill · {inputs.get('skill') or 'unknown'}"
    if name in {"WebSearch", "WebFetch"}:
        target = str(inputs.get("query") or inputs.get("url") or "web")
        return f"{name} · {truncate(target, 72)}"
    return name


def tool_action_detail(name: str, inputs: dict[str, Any]) -> str:
    """Return the unabridged input that is most useful when inspecting an action."""
    if name == "Bash":
        return str(inputs.get("command") or "")
    if name in {"Read", "Write", "Edit"}:
        return str(inputs.get("file_path") or inputs.get("path") or "")
    if name in {"Glob", "Grep"}:
        return str(inputs.get("pattern") or inputs.get("query") or "")
    if name in {"WebSearch", "WebFetch"}:
        return str(inputs.get("query") or inputs.get("url") or "")
    return json.dumps(inputs, ensure_ascii=False, indent=2) if inputs else ""


def compact_action_output(value: str, max_lines: int = 24, max_chars: int = 12_000) -> str:
    """Bound stored tool output while retaining useful evidence from both ends."""
    value = re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))", "", value)
    if len(value) > max_chars:
        tail_chars = max_chars // 4
        value = (
            value[:max_chars - tail_chars]
            + "\n… output truncated …\n"
            + value[-tail_chars:]
        )
    lines = value.splitlines()
    if len(lines) <= max_lines:
        return value
    head_count = max_lines - 6
    omitted = len(lines) - max_lines
    return "\n".join(lines[:head_count] + [f"… {omitted} lines omitted …"] + lines[-6:])


def attach_action_output(prompt: PromptTurn, inputs: dict[str, Any], output: str) -> None:
    request_index = inputs.get("__request_index")
    action_index = inputs.get("__action_index")
    if not isinstance(request_index, int) or not isinstance(action_index, int):
        return
    if not 0 <= request_index < len(prompt.requests):
        return
    request = prompt.requests[request_index]
    while len(request.action_outputs) < len(request.actions):
        request.action_outputs.append("")
    if action_index < len(request.action_outputs):
        request.action_outputs[action_index] = compact_action_output(output)


def tool_result_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            str(item.get("text") or item.get("content") or "")
            for item in value if isinstance(item, dict)
        )
    return json.dumps(value, ensure_ascii=False) if value is not None else ""


def embedded_json(text: str) -> dict[str, Any] | None:
    cleaned = "\n".join(re.sub(r"^\s*\d+[→\t]", "", line) for line in text.splitlines())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        value = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def attach_codex_telemetry(
    path: str,
    result_text: str,
    tools_by_id: dict[str, tuple[PromptTurn, str, dict[str, Any]]],
) -> None:
    telemetry = embedded_json(result_text)
    if not telemetry or telemetry.get("kind") != "codex_exec_telemetry":
        return
    prompts = list(dict.fromkeys(id(owner) for owner, _, _ in tools_by_id.values()))
    owners = []
    for owner, _, _ in tools_by_id.values():
        if id(owner) in prompts:
            owners.append(owner)
            prompts.remove(id(owner))
    candidates = [actor for owner in owners for actor in owner.actors]
    actor = next(
        (item for item in reversed(candidates) if item.telemetry_path and path.endswith(item.telemetry_path)),
        next((item for item in reversed(candidates) if not item.token_usage), None),
    )
    if not actor:
        return
    actor.thread_id = telemetry.get("thread_id")
    actor.duration_seconds = telemetry.get("duration_seconds")
    actor.token_usage = {
        key: int(value) for key, value in (telemetry.get("usage") or {}).items()
        if isinstance(value, (int, float))
    }
    actor.rate_limits = telemetry.get("rate_limits") if isinstance(telemetry.get("rate_limits"), dict) else None


def parse_iso_timestamp(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_actor_telemetry(actor: Actor) -> None:
    """Load a matching sidecar directly; never route telemetry through Claude text."""
    if not actor.working_dir:
        return
    candidates: list[Path] = []
    if actor.telemetry_path and "$" not in actor.telemetry_path:
        path = Path(actor.telemetry_path).expanduser()
        candidates.append(path if path.is_absolute() else Path(actor.working_dir) / path)
    candidates.extend(Path(actor.working_dir).glob("logs/codex-*.telemetry.json"))
    finished = parse_iso_timestamp(actor.finished_at)
    best: tuple[float, dict[str, Any]] | None = None
    for path in set(candidates):
        try:
            telemetry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(telemetry, dict) or telemetry.get("kind") != "codex_exec_telemetry":
            continue
        telemetry_finished = parse_iso_timestamp(telemetry.get("finished_at"))
        distance = abs((finished - telemetry_finished).total_seconds()) if finished and telemetry_finished else float("inf")
        if distance <= 300 and (best is None or distance < best[0]):
            best = (distance, telemetry)
    if not best:
        return
    telemetry = best[1]
    actor.thread_id = telemetry.get("thread_id")
    actor.duration_seconds = telemetry.get("duration_seconds")
    actor.token_usage = {
        key: int(value) for key, value in (telemetry.get("usage") or {}).items()
        if isinstance(value, (int, float))
    }
    actor.rate_limits = telemetry.get("rate_limits") if isinstance(telemetry.get("rate_limits"), dict) else None


def is_agy_command(command: str) -> bool:
    for segment in re.split(r"&&|\|\||[;|\n]", command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            continue
        executable = next((token for token in tokens if not re.match(r"^[A-Za-z_]\w*=", token)), "")
        executable_name = Path(executable).name
        if executable_name == "agy" and any(arg in {"-p", "--print", "--prompt"} for arg in tokens[1:]):
            return True
        if executable_name == "spawn-agy.sh" and any(
            arg == "--prompt-file" or arg.startswith("--prompt-file=") for arg in tokens[1:]
        ):
            return True
    return False


def spawned_agent_engine(command: str) -> str | None:
    """Return the managed agent engine, excluding ordinary yielded shell commands."""
    if is_agy_command(command):
        return "agy"
    script_engines = {
        "spawn-claude.sh": "claude",
        "claude-background-bridge.py": "claude",
        "claude-background-bridge.mjs": "claude",
        "spawn-codex.sh": "codex",
        "spawn-advise.sh": "codex",
    }
    for segment in re.split(r"&&|\|\||[;|\n]", command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            continue
        tokens = [token for token in tokens if not re.match(r"^[A-Za-z_]\w*=", token)]
        if not tokens:
            continue
        executable = Path(tokens[0]).name
        candidate = executable
        candidate_index = 0
        if executable in {"bash", "sh", "zsh"}:
            if "-n" in tokens[1:]:
                continue
            candidate_index = next((index for index, token in enumerate(tokens[1:], 1) if not token.startswith("-")), -1)
            candidate = Path(tokens[candidate_index]).name if candidate_index >= 0 else ""
        elif executable.startswith("python"):
            candidate_index = next((index for index, token in enumerate(tokens[1:], 1) if not token.startswith("-")), -1)
            candidate = Path(tokens[candidate_index]).name if candidate_index >= 0 else ""
        engine = script_engines.get(candidate)
        invocation_args = tokens[candidate_index + 1:] if candidate_index >= 0 else []
        if engine and not any(flag in invocation_args for flag in ("--help", "-h", "--dry-run")):
            return engine
    return None


def command_option(command: str, option: str) -> str | None:
    match = re.search(
        rf"{re.escape(option)}(?:=|\s+)(?:\"([^\"]+)\"|'([^']+)'|([^\s;&|]+))",
        command,
    )
    return next((value for value in match.groups() if value), None) if match else None


def agy_model(command: str) -> str:
    return command_option(command, "--model") or "default"


def load_agy_decision(actor: Actor) -> None:
    if actor.engine != "agy" or not actor.working_dir:
        return
    path = Path(actor.working_dir) / "logs" / "agy-decisions.jsonl"
    if not path.is_file():
        return
    started = parse_iso_timestamp(actor.started_at)
    finished = parse_iso_timestamp(actor.finished_at)
    best: tuple[float, dict[str, Any]] | None = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict) or item.get("engine") != "agy":
            continue
        timestamp = parse_iso_timestamp(item.get("ts"))
        reference = finished or started
        distance = abs((reference - timestamp).total_seconds()) if reference and timestamp else float("inf")
        if distance <= 600 and (best is None or distance < best[0]):
            best = (distance, item)
    if not best:
        return
    item = best[1]
    actor.model = str(item.get("model") or actor.model or "default")
    actor.mode = str(item.get("mode") or "") or None
    actor.lane = str(item.get("lane") or "") or None
    actor.verdict = str(item.get("verdict") or "") or None
    actor.decision = str(item.get("decision") or "") or None
    actor.rationale = str(item.get("rationale") or "") or None


def enrich_record(
    prompt: PromptTurn,
    rec: dict[str, Any],
    tools_by_id: dict[str, tuple[PromptTurn, str, dict[str, Any]]],
) -> None:
    """Extract profiler evidence without changing token accounting."""
    timestamp = rec.get("timestamp")
    content = message_content(rec)
    text = content_text(content)
    rec_type = str(rec.get("type") or "")

    observation = parse_usage_observation(text) if rec_type == "user" else None
    if observation:
        prompt.usage_observations.append(observation)
        prompt.timeline.append(TimelineItem(timestamp, "Usage observation recorded", "usage"))
        return

    if rec.get("isCompactSummary") or str(rec.get("subtype") or "") == "compact_boundary":
        # Compaction resets the window, so accumulated evidence no longer describes it.
        prompt.evidence_chars.clear()
        prompt.evidence_chars["conversation"] += local_token_weight(text)
        prompt.events.append("Context compacted")
        prompt.timeline.append(TimelineItem(timestamp, "Context compacted", "context"))
        return

    if rec_type == "attachment":
        attachment = rec.get("attachment") if isinstance(rec.get("attachment"), dict) else {}
        category = ATTACHMENT_EVIDENCE.get(str(attachment.get("type") or ""), "system")
        prompt.evidence_chars[category] += local_token_weight(json.dumps(attachment, ensure_ascii=False))
        return

    if rec_type == "system":
        payload = rec.get("content")
        if not isinstance(payload, str):
            payload = json.dumps(payload, ensure_ascii=False) if payload else ""
        prompt.evidence_chars["system"] += local_token_weight(payload)
        return

    if rec_type == "user" and not is_synthetic(rec) and not has_tool_result(content):
        category = "skills" if text.startswith("Base directory for this skill:") else "conversation"
        prompt.evidence_chars[category] += local_token_weight(text)
    elif rec_type == "assistant":
        prompt.evidence_chars["conversation"] += local_token_weight(text)

    usage_tuple = extract_usage(rec)
    request_info: RequestInfo | None = None
    if usage_tuple:
        model, inp, out, cache_create, cache_read = usage_tuple
        request_key = usage_identity(rec)
        request_info = next((item for item in prompt.requests if item.request_id == request_key), None)
        if not request_info:
            request_usage = Usage()
            request_usage.add(model, inp, out, cache_create, cache_read)
            msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
            request_info = RequestInfo(
                timestamp, model, request_usage, request_id=request_key,
                stop_reason=msg.get("stop_reason"), last_timestamp=timestamp,
            )
            prompt.requests.append(request_info)
            if request_key:
                prompt.request_info_keys.add(request_key)
        else:
            request_info.last_timestamp = timestamp or request_info.last_timestamp

    for block in content_blocks(content, "tool_use"):
        tool_id = str(block.get("id") or "")
        name = str(block.get("name") or "Tool")
        inputs = block.get("input") if isinstance(block.get("input"), dict) else {}
        activity = tool_activity_label(name, inputs)
        prompt.timeline.append(TimelineItem(timestamp, f"{activity} — started", "tool"))
        tool_inputs = dict(inputs)
        if request_info:
            existing = tools_by_id.get(tool_id)
            existing_inputs = existing[2] if existing and existing[0] is prompt else {}
            if isinstance(existing_inputs.get("__action_index"), int):
                action_index = existing_inputs["__action_index"]
            else:
                action_index = len(request_info.actions)
                request_info.actions.append(activity)
                request_info.action_details.append(tool_action_detail(name, inputs))
                request_info.action_outputs.append("")
            tool_inputs["__request_index"] = prompt.requests.index(request_info)
            tool_inputs["__action_index"] = action_index
        tools_by_id[tool_id] = (prompt, name, tool_inputs)

        action = FILE_TOOL_ACTIONS.get(name)
        if action:
            path = inputs.get("file_path") or inputs.get("path") or inputs.get("pattern")
            if path:
                prompt.files.append(FileActivity(str(path), action, timestamp))

        if name == "Skill":
            prompt.evidence_chars["skills"] += local_token_weight(json.dumps(inputs, ensure_ascii=False))

        command = str(inputs.get("command") or "") if name == "Bash" else ""
        agy_run = name == "Bash" and is_agy_command(command)
        if name == "Bash" and (inputs.get("run_in_background") or agy_run):
            description = str(inputs.get("description") or ("Antigravity review" if agy_run else "Background command"))
            label = f"Gemini · {description}" if agy_run else description
            telemetry_match = re.search(r"\bTELEMETRY=(?:\"([^\"]+)\"|'([^']+)'|(\S+))", command)
            telemetry_path = next((value for value in telemetry_match.groups() if value), None) if telemetry_match else None
            prompt.actors.append(Actor(
                tool_id, label, "running", timestamp,
                tool_use_id=tool_id, telemetry_path=telemetry_path,
                working_dir=(command_option(command, "--project") if agy_run else None)
                or str(rec.get("cwd") or ""),
                engine="agy" if agy_run else None,
                model=agy_model(command) if agy_run else None,
            ))
            if agy_run:
                prompt.timeline.append(TimelineItem(timestamp, f"Actor · {label} — started", "actor"))

    for block in content_blocks(content, "tool_result"):
        tool_id = str(block.get("tool_use_id") or "")
        linked = tools_by_id.get(tool_id)
        result_text = tool_result_text(block.get("content"))
        if linked:
            owner, name, inputs = linked
            activity = tool_activity_label(name, inputs)
            background_running = name == "Bash" and "running in background with ID" in result_text
            state = "failed" if block.get("is_error") else ("running" if background_running else "completed")
            owner.timeline.append(TimelineItem(timestamp, f"{activity} — {state}", "tool"))
            attach_action_output(owner, inputs, result_text)
            target_path = str(inputs.get("file_path") or inputs.get("path") or "")
            if target_path.endswith(AGENT_INSTRUCTION_FILES):
                owner.evidence_chars["agents"] += local_token_weight(result_text)
            elif name in {"Read", "Glob", "Grep"}:
                owner.evidence_chars["repository"] += local_token_weight(result_text)
            elif name == "Skill":
                owner.evidence_chars["skills"] += local_token_weight(result_text)
            else:
                owner.evidence_chars["tool output"] += local_token_weight(result_text)
            if name == "Read":
                telemetry_path = str(inputs.get("file_path") or "")
                attach_codex_telemetry(telemetry_path, result_text, tools_by_id)
            actor = actor_for_tool(owner, tool_id)
            if actor:
                if result_text.strip():
                    actor.log_chunks.append(result_text)
                task_match = re.search(r"background with ID:\s*([\w-]+)", result_text, re.I)
                wrapper_exit = re.search(r"(?:^|\n)exit_code=(-?\d+)(?:\n|$)", result_text)
                for field_name, attr_name in (("output", "output_path"), ("events", "events_path"), ("telemetry", "telemetry_path")):
                    match = re.search(rf"(?:^|\n){field_name}=([^\n]+)", result_text)
                    if match:
                        setattr(actor, attr_name, match.group(1).strip())
                if wrapper_exit:
                    actor.exit_code = int(wrapper_exit.group(1))
                if task_match:
                    actor.task_id = task_match.group(1)
                elif block.get("is_error") or (actor.exit_code is not None and actor.exit_code != 0):
                    actor.status, actor.finished_at = "failed", timestamp
                elif not background_running:
                    actor.status, actor.finished_at = "completed", timestamp
                if actor.engine == "agy" and actor.finished_at:
                    load_agy_decision(actor)
                    owner.timeline.append(
                        TimelineItem(timestamp, f"Actor · {actor.label} — {actor.status}", "actor")
                    )

    if "<task-notification>" in text.lower() or "<task_notification>" in text.lower():
        tool_match = re.search(r"<tool-use-id>\s*([^<]+)", text, re.I)
        task_match = re.search(r"<task-id>\s*([^<]+)", text, re.I)
        status_match = re.search(r"<status>\s*([^<]+)", text, re.I)
        summary_match = re.search(r"<summary>\s*([^<]+)", text, re.I)
        exit_match = re.search(r"exit code\s+(-?\d+)", text, re.I)
        tool_id = tool_match.group(1).strip() if tool_match else ""
        linked = tools_by_id.get(tool_id)
        owner = linked[0] if linked else prompt
        actor = actor_for_tool(owner, tool_id)
        if not actor:
            label = summary_match.group(1).strip() if summary_match else "Background task"
            actor = Actor(tool_id or f"task:{len(owner.actors)}", label, tool_use_id=tool_id or None)
            owner.actors.append(actor)
        actor.status = status_match.group(1).strip() if status_match else "completed"
        actor.finished_at = timestamp
        actor.task_id = task_match.group(1).strip() if task_match else actor.task_id
        actor.exit_code = int(exit_match.group(1)) if exit_match else None
        actor.log_chunks.append(text)
        load_actor_telemetry(actor)
        owner.timeline.append(TimelineItem(timestamp, f"Actor · {actor.label} — {actor.status}", "actor"))
        owner.evidence_chars["tool output"] += local_token_weight(text)


def enrich_analysis(path: str, analysis: Analysis) -> dict[str, tuple[PromptTurn, str, dict[str, Any]]]:
    tools: dict[str, tuple[PromptTurn, str, dict[str, Any]]] = {}
    current: PromptTurn | None = None
    # Session preamble (system prompt, skill/tool listings) lands before the first prompt;
    # collect it here so prompt 1 inherits it instead of dropping it on the floor.
    base = PromptTurn(index=0, prompt="", start_sequence=0, timestamp=None)
    prompt_position = 0
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            if is_real_user_prompt(rec):
                if prompt_position < len(analysis.prompts):
                    previous = current
                    if current:
                        current.timeline.append(TimelineItem(rec.get("timestamp"), "Prompt completed", "prompt"))
                    current = analysis.prompts[prompt_position]
                    prompt_position += 1
                    current.evidence_chars.update(
                        previous.evidence_chars if previous else base.evidence_chars
                    )
                    current.timeline.append(TimelineItem(rec.get("timestamp"), "Prompt started", "prompt"))
                    prompt_value = content_text(message_content(rec))
                    category = "instructions" if prompt_value.startswith("Base directory for this skill:") else "conversation"
                    current.evidence_chars[category] += local_token_weight(prompt_value)
                continue
            enrich_record(current or base, rec, tools)
    return tools


class IncrementalSessionAnalyzer:
    """Parse an existing session once, then consume only newly appended JSONL bytes."""
    def __init__(self, path: str) -> None:
        self.path = path
        self.analysis = analyze(path)
        self.tools = enrich_analysis(path, self.analysis)
        self.offset = os.path.getsize(path)
        self.sequence = self.analysis.record_count
        self.partial = ""

    def poll(self) -> bool:
        size = os.path.getsize(self.path)
        if size < self.offset:
            self.__init__(self.path)
            return True
        if size == self.offset:
            return False
        with open(self.path, "r", encoding="utf-8") as fh:
            fh.seek(self.offset)
            chunk = fh.read()
            self.offset = fh.tell()
        data = self.partial + chunk
        lines = data.splitlines(keepends=True)
        self.partial = ""
        if lines and not lines[-1].endswith(("\n", "\r")):
            self.partial = lines.pop()
        changed = False
        for line in lines:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                self.analysis.malformed += 1
                continue
            if not isinstance(rec, dict):
                continue
            self.sequence += 1
            self.analysis.record_count += 1
            self._append_record(rec)
            changed = True
        return changed

    def _append_record(self, rec: dict[str, Any]) -> None:
        if is_real_user_prompt(rec):
            previous = self.analysis.prompts[-1] if self.analysis.prompts else None
            if self.analysis.prompts:
                self.analysis.prompts[-1].timeline.append(TimelineItem(rec.get("timestamp"), "Prompt completed", "prompt"))
                for actor in self.analysis.prompts[-1].actors:
                    if actor.key == "main" and actor.status == "running":
                        actor.status = "completed"
            prompt = PromptTurn(len(self.analysis.prompts) + 1, prompt_text(rec), self.sequence, rec.get("timestamp"))
            if previous:
                prompt.evidence_chars.update(previous.evidence_chars)
            prompt.timeline.append(TimelineItem(rec.get("timestamp"), "Prompt started", "prompt"))
            prompt.evidence_chars["conversation"] += local_token_weight(content_text(message_content(rec)))
            self.analysis.prompts.append(prompt)
            return
        current = self.analysis.prompts[-1] if self.analysis.prompts else None
        if not current:
            return
        label = event_label(rec)
        if label:
            current.events.append(label)
        usage_tuple = extract_usage(rec)
        if usage_tuple:
            usage_key = usage_identity(rec)
            if not usage_key or usage_key not in current.accounted_usage_keys:
                if usage_key:
                    current.accounted_usage_keys.add(usage_key)
                model, inp, out, cache_create, cache_read = usage_tuple
                target = Usage()
                target.add(model, inp, out, cache_create, cache_read)
                if rec.get("isSidechain"):
                    key = sub_session_key(rec, current.index, self.sequence)
                    sub = current.sub_sessions.setdefault(key, SubSession(key, f"{short_model(model)} sub-session", self.sequence))
                    sub.usage.merge(target)
                else:
                    current.main.merge(target)
        enrich_record(current, rec, self.tools)


CODEX_TOOL_ACTIONS = {
    "exec_command": "Bash",
    "apply_patch": "Edit",
    "view_image": "Read",
    "web__run": "WebSearch",
    "image_gen__imagegen": "Write",
    "read_mcp_resource": "Read",
}


def codex_tool_activity(payload: dict[str, Any]) -> tuple[str, str]:
    raw = str(payload.get("input") or "")
    method_match = re.search(r"\btools\.([A-Za-z0-9_]+)\s*\(", raw)
    method = method_match.group(1) if method_match else str(payload.get("name") or "Tool")
    action = CODEX_TOOL_ACTIONS.get(method, method.replace("__", " ").replace("_", " ").title())
    description = ""
    if method == "exec_command":
        command = codex_exec_command(payload)
        description = command[:100].replace("\\n", " ") if command else "Run command"
    elif method == "apply_patch":
        description = "Apply patch"
    elif method == "web__run":
        description = "Search the web"
    elif method:
        description = method.replace("__", " ").replace("_", " ")
    return action, truncate(description, 90)


def codex_exec_command(payload: dict[str, Any]) -> str:
    raw_input = payload.get("input")
    if isinstance(raw_input, dict):
        return str(raw_input.get("cmd") or "")
    raw = str(raw_input or "")
    match = re.search(r'''(?:["']cmd["']|\bcmd)\s*:\s*(["'])(.*?)\1(?:\s*[,}])''', raw, re.S)
    return match.group(2).replace("\\n", "\n") if match else ""


def codex_tool_detail(payload: dict[str, Any]) -> str:
    """Keep complete tool input for the request detail page."""
    command = codex_exec_command(payload)
    if command:
        return command
    raw_input = payload.get("input")
    if isinstance(raw_input, dict):
        return json.dumps(raw_input, ensure_ascii=False, indent=2)
    return str(raw_input or "")


def codex_message_text(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return " ".join(
        str(block.get("text") or block.get("input_text") or block.get("output_text") or "")
        for block in content if isinstance(block, dict)
    )


def codex_function_output_text(payload: dict[str, Any]) -> str:
    output = payload.get("output")
    if isinstance(output, str):
        return output
    if not isinstance(output, list):
        return ""
    return "\n".join(
        str(block.get("text") or "") for block in output if isinstance(block, dict)
    )


class CodexSessionAnalyzer:
    """Incrementally normalize Codex rollout JSONL into the shared profiler model."""
    def __init__(self, path: str) -> None:
        self.path = path
        self.analysis = Analysis(path, [], Usage(), 0, 0, provider="codex")
        self.tools: dict[str, tuple[PromptTurn, str, dict[str, Any]]] = {}
        self.offset = 0
        self.sequence = 0
        self.partial = ""
        self.model = "codex"
        self.pending_actions: list[str] = []
        self.pending_action_details: list[str] = []
        self.pending_action_outputs: list[str] = []
        self.pending_cells: dict[str, Actor] = {}
        self.base_evidence: Counter[str] = Counter()
        self.poll()

    def poll(self) -> bool:
        size = os.path.getsize(self.path)
        if size < self.offset:
            self.__init__(self.path)
            return True
        if size == self.offset:
            return False
        with open(self.path, "r", encoding="utf-8") as fh:
            fh.seek(self.offset)
            chunk = fh.read()
            self.offset = fh.tell()
        data = self.partial + chunk
        lines = data.splitlines(keepends=True)
        self.partial = ""
        if lines and not lines[-1].endswith(("\n", "\r")):
            self.partial = lines.pop()
        changed = False
        for line in lines:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                self.analysis.malformed += 1
                continue
            if not isinstance(rec, dict):
                continue
            self.sequence += 1
            self.analysis.record_count += 1
            self._append_record(rec)
            changed = True
        return changed

    def _append_record(self, rec: dict[str, Any]) -> None:
        payload = rec.get("payload")
        if not isinstance(payload, dict):
            return
        kind = str(payload.get("type") or "")
        timestamp = rec.get("timestamp")
        if rec.get("type") == "session_meta":
            system_payload = str(payload.get("base_instructions") or "")
            dynamic_tools = payload.get("dynamic_tools")
            if dynamic_tools:
                system_payload += json.dumps(dynamic_tools, ensure_ascii=False)
            self.base_evidence["system"] += local_token_weight(system_payload)
            self.base_evidence["agents"] += local_token_weight(str(payload.get("user_instructions") or ""))
            return
        if rec.get("type") == "turn_context":
            self.model = str(payload.get("model") or self.model)
            return
        if kind == "item_completed":
            item = payload.get("item")
            if not isinstance(item, dict) or str(item.get("type") or "").lower() != "usermessage":
                return
            payload = {"type": "user_message", "message": codex_message_text(item)}
            kind = "user_message"
        if kind == "message" and str(payload.get("role") or "") == "developer":
            text = codex_message_text(payload)
            category = "agents" if "AGENTS.md" in text or "<INSTRUCTIONS>" in text else "system"
            self.base_evidence[category] += local_token_weight(text)
            return
        if kind == "user_message":
            if self.analysis.prompts:
                previous = self.analysis.prompts[-1]
                if not any(item.label == "Prompt completed" for item in previous.timeline):
                    previous.timeline.append(TimelineItem(timestamp, "Prompt completed", "prompt"))
            text = truncate(str(payload.get("message") or "(empty prompt)"), 120)
            prompt = PromptTurn(len(self.analysis.prompts) + 1, text, self.sequence, timestamp)
            if self.analysis.prompts:
                prompt.evidence_chars.update(self.analysis.prompts[-1].evidence_chars)
            else:
                prompt.evidence_chars.update(self.base_evidence)
            prompt.timeline.append(TimelineItem(timestamp, "Prompt started", "prompt"))
            prompt.evidence_chars["conversation"] += local_token_weight(str(payload.get("message") or ""))
            self.analysis.prompts.append(prompt)
            self.pending_actions = []
            self.pending_action_details = []
            self.pending_action_outputs = []
            return
        prompt = self.analysis.prompts[-1] if self.analysis.prompts else None
        if not prompt:
            return
        if kind == "custom_tool_call":
            action, description = codex_tool_activity(payload)
            activity = f"{action} · {description}" if description else action
            call_id = str(payload.get("call_id") or payload.get("id") or self.sequence)
            prompt.timeline.append(TimelineItem(timestamp, f"{activity} — started", "tool"))
            self.pending_actions.append(activity)
            self.pending_action_details.append(codex_tool_detail(payload))
            self.pending_action_outputs.append("")
            command = codex_exec_command(payload) if action == "Bash" else ""
            self.tools[call_id] = (
                prompt, activity,
                {"command": command, "__action_index": len(self.pending_actions) - 1},
            )
            engine = spawned_agent_engine(command)
            if engine:
                display_name = {"agy": "Gemini", "claude": "Claude", "codex": "Codex"}[engine]
                label = f"{display_name} · {description or 'Background agent'}"
                prompt.actors.append(Actor(
                    call_id, label, "running", timestamp,
                    tool_use_id=call_id,
                    working_dir=command_option(command, "--project") or "",
                    engine=engine, model=agy_model(command) if engine == "agy" else None,
                ))
                prompt.timeline.append(TimelineItem(timestamp, f"Actor · {label} — started", "actor"))
            path_match = re.search(r"(?:/Users/|/private/|/tmp/)[^\s\"']+", str(payload.get("input") or ""))
            if path_match and action.lower() in {"write", "edit", "read"}:
                prompt.files.append(FileActivity(path_match.group(0), action.lower(), timestamp))
            return
        if kind == "custom_tool_call_output":
            call_id = str(payload.get("call_id") or "")
            linked = self.tools.get(call_id)
            if linked:
                owner, activity, inputs = linked
                owner.timeline.append(TimelineItem(timestamp, f"{activity} — completed", "tool"))
                result_text = codex_function_output_text(payload)
                action_index = inputs.get("__action_index")
                if isinstance(action_index, int) and action_index < len(self.pending_action_outputs):
                    self.pending_action_outputs[action_index] = compact_action_output(result_text)
                attach_action_output(owner, inputs, result_text)
                category = "repository" if activity.startswith(("Read ·", "Glob ·", "Grep ·")) else "tool output"
                owner.evidence_chars[category] += local_token_weight(result_text)
                actor = actor_for_tool(owner, call_id)
                cell = re.search(r"Script running with cell ID\s+([\w-]+)", result_text)
                if actor and result_text.strip():
                    actor.log_chunks.append(result_text)
                if actor:
                    if cell:
                        actor.task_id = cell.group(1)
                        self.pending_cells[cell.group(1)] = actor
                    else:
                        self._complete_background_actor(actor, result_text, timestamp, owner)
            return
        if kind == "function_call" and payload.get("name") == "wait":
            call_id = str(payload.get("call_id") or payload.get("id") or self.sequence)
            try:
                arguments = json.loads(str(payload.get("arguments") or "{}"))
            except json.JSONDecodeError:
                arguments = {}
            cell_id = str(arguments.get("cell_id") or "")
            self.tools[call_id] = (prompt, "Wait · process", {"cell_id": cell_id})
            return
        if kind == "function_call_output":
            call_id = str(payload.get("call_id") or "")
            linked = self.tools.get(call_id)
            if linked and linked[1] == "Wait · process":
                owner, _, inputs = linked
                cell_id = str(inputs.get("cell_id") or "")
                actor = self.pending_cells.get(cell_id)
                if actor:
                    output = codex_function_output_text(payload)
                    actor.log_chunks.append(output)
                    if "Script running with cell ID" not in output:
                        self.pending_cells.pop(cell_id, None)
                        self._complete_background_actor(actor, output, timestamp, owner)
            return
        if kind == "agent_message":
            prompt.evidence_chars["conversation"] += local_token_weight(str(payload.get("message") or ""))
            return
        if kind == "token_count":
            info = payload.get("info")
            last = info.get("last_token_usage") if isinstance(info, dict) else None
            if not isinstance(last, dict):
                return
            total_input = int(last.get("input_tokens", 0) or 0)
            cached = int(last.get("cached_input_tokens", 0) or 0)
            output = int(last.get("output_tokens", 0) or 0)
            usage = Usage()
            usage.add(self.model, max(0, total_input - cached), output, 0, cached)
            prompt.main.merge(usage)
            request_index = len(prompt.requests)
            prompt.requests.append(RequestInfo(
                timestamp, self.model, usage, request_id=f"codex:{self.sequence}",
                stop_reason="turn", actions=self.pending_actions,
                action_details=self.pending_action_details,
                action_outputs=self.pending_action_outputs,
            ))
            for owner, _, inputs in self.tools.values():
                if owner is prompt and "__action_index" in inputs and "__request_index" not in inputs:
                    inputs["__request_index"] = request_index
            self.pending_actions = []
            self.pending_action_details = []
            self.pending_action_outputs = []
            return
        if kind == "task_complete":
            prompt.timeline.append(TimelineItem(timestamp, "Prompt completed", "prompt"))
            return
        if kind == "turn_aborted":
            prompt.events.append("Interrupted by user")
            prompt.timeline.append(TimelineItem(timestamp, "Prompt interrupted", "prompt"))
            return
        if kind == "context_compacted":
            prompt.events.append("Context compacted")
            prompt.timeline.append(TimelineItem(timestamp, "Context compacted", "context"))
            return

    def _complete_agy_actor(
        self, actor: Actor, result_text: str, timestamp: str | None, owner: PromptTurn,
    ) -> None:
        wrapper_exit = re.search(r"(?:exit_code=|\"exit_code\"\s*:\s*)(-?\d+)", result_text)
        actor.exit_code = int(wrapper_exit.group(1)) if wrapper_exit else None
        actor.status = "failed" if actor.exit_code not in {None, 0} else "completed"
        actor.finished_at = timestamp
        load_agy_decision(actor)
        owner.timeline.append(TimelineItem(timestamp, f"Actor · {actor.label} — {actor.status}", "actor"))

    def _complete_background_actor(
        self, actor: Actor, result_text: str, timestamp: str | None, owner: PromptTurn,
    ) -> None:
        wrapper_exit = re.search(r"(?:exit_code=|\"exit_code\"\s*:\s*)(-?\d+)", result_text)
        actor.exit_code = int(wrapper_exit.group(1)) if wrapper_exit else None
        actor.status = "failed" if actor.exit_code not in {None, 0} else "completed"
        actor.finished_at = timestamp
        for field_name, attr_name in (("output", "output_path"), ("events", "events_path"), ("telemetry", "telemetry_path")):
            match = re.search(rf"(?:^|\n){field_name}=([^\n]+)", result_text)
            if match:
                setattr(actor, attr_name, match.group(1).strip())
        if actor.engine == "agy":
            load_agy_decision(actor)
        owner.timeline.append(TimelineItem(timestamp, f"Actor · {actor.label} — {actor.status}", "actor"))


def create_analyzer(path: str) -> IncrementalSessionAnalyzer | CodexSessionAnalyzer:
    return CodexSessionAnalyzer(path) if session_provider(path) == "codex" else IncrementalSessionAnalyzer(path)


CACHE_TYPES = {
    cls.__name__: cls for cls in (
        Usage, SubSession, Actor, TimelineItem, FileActivity, RequestInfo,
        UsageObservation, PromptTurn, Analysis,
    )
}


def cache_encode(value: Any) -> Any:
    if is_dataclass(value):
        return {
            "__type__": type(value).__name__,
            **{item.name: cache_encode(getattr(value, item.name)) for item in fields(value)},
        }
    if isinstance(value, Counter):
        return {"__counter__": dict(value)}
    if isinstance(value, set):
        return {"__set__": sorted(value)}
    if isinstance(value, dict):
        return {str(key): cache_encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [cache_encode(item) for item in value]
    return value


def cache_decode(value: Any) -> Any:
    if isinstance(value, list):
        return [cache_decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    if "__counter__" in value:
        return Counter(value["__counter__"])
    if "__set__" in value:
        return set(value["__set__"])
    type_name = value.get("__type__")
    if type_name in CACHE_TYPES:
        cls = CACHE_TYPES[type_name]
        return cls(**{
            key: cache_decode(item) for key, item in value.items() if key != "__type__"
        })
    return {key: cache_decode(item) for key, item in value.items()}


class ProfilerCache:
    """Persistent materialized parser state, resumed from the source byte offset."""
    def __init__(self, path: str | None = None) -> None:
        default = Path.home() / "Library" / "Caches" / "execution-profiler" / "profiler.sqlite3"
        self.path = Path(path or os.environ.get("EXECUTION_PROFILER_CACHE") or default)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS materialized_sessions (
                source_path TEXT PRIMARY KEY,
                source_inode INTEGER NOT NULL,
                byte_offset INTEGER NOT NULL,
                source_size INTEGER NOT NULL,
                source_mtime_ns INTEGER NOT NULL,
                schema_version INTEGER NOT NULL,
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        self.db.commit()

    def load(self, path: str) -> IncrementalSessionAnalyzer | CodexSessionAnalyzer | None:
        stat = os.stat(path)
        row = self.db.execute(
            "SELECT source_inode, byte_offset, schema_version, state_json "
            "FROM materialized_sessions WHERE source_path = ?", (str(Path(path).resolve()),),
        ).fetchone()
        if not row or row[0] != stat.st_ino or row[1] > stat.st_size or row[2] != CACHE_SCHEMA_VERSION:
            return None
        try:
            state = json.loads(row[3])
            analysis = cache_decode(state["analysis"])
            if not isinstance(analysis, Analysis):
                return None
            analyzer_class = CodexSessionAnalyzer if analysis.provider == "codex" else IncrementalSessionAnalyzer
            analyzer = analyzer_class.__new__(analyzer_class)
            analyzer.path = path
            analyzer.analysis = analysis
            analyzer.offset = int(state["offset"])
            analyzer.sequence = int(state["sequence"])
            analyzer.partial = str(state.get("partial") or "")
            if isinstance(analyzer, CodexSessionAnalyzer):
                analyzer.model = str(state.get("model") or (
                    analysis.prompts[-1].main.primary_model if analysis.prompts else "codex"
                ))
                analyzer.pending_actions = list(state.get("pending_actions") or [])
                analyzer.pending_action_details = list(state.get("pending_action_details") or [])
                analyzer.pending_action_outputs = list(state.get("pending_action_outputs") or [])
                analyzer.pending_cells = {
                    actor.task_id: actor
                    for prompt in analysis.prompts
                    for actor in prompt.actors
                    if actor.status == "running" and actor.task_id
                }
                analyzer.base_evidence = Counter(state.get("base_evidence") or {})
            analyzer.tools = {}
            for tool_id, tool in state.get("tools", {}).items():
                prompt = next((item for item in analysis.prompts if item.index == tool[0]), None)
                if prompt:
                    analyzer.tools[tool_id] = (prompt, tool[1], tool[2])
            return analyzer
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def save(self, analyzer: IncrementalSessionAnalyzer | CodexSessionAnalyzer) -> bool:
        stat = os.stat(analyzer.path)
        tools = {
            tool_id: [prompt.index, name, inputs]
            for tool_id, (prompt, name, inputs) in analyzer.tools.items()
        }
        state = json.dumps({
            "analysis": cache_encode(analyzer.analysis),
            "offset": analyzer.offset,
            "sequence": analyzer.sequence,
            "partial": analyzer.partial,
            "tools": tools,
            "model": getattr(analyzer, "model", None),
            "pending_actions": getattr(analyzer, "pending_actions", []),
            "pending_action_details": getattr(analyzer, "pending_action_details", []),
            "pending_action_outputs": getattr(analyzer, "pending_action_outputs", []),
            "base_evidence": dict(getattr(analyzer, "base_evidence", {})),
        }, ensure_ascii=False, separators=(",", ":"))
        try:
            self.db.execute("""
                INSERT INTO materialized_sessions (
                    source_path, source_inode, byte_offset, source_size, source_mtime_ns,
                    schema_version, state_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_path) DO UPDATE SET
                    source_inode=excluded.source_inode, byte_offset=excluded.byte_offset,
                    source_size=excluded.source_size, source_mtime_ns=excluded.source_mtime_ns,
                    schema_version=excluded.schema_version, state_json=excluded.state_json,
                    updated_at=excluded.updated_at
            """, (
                str(Path(analyzer.path).resolve()), stat.st_ino, analyzer.offset, stat.st_size,
                stat.st_mtime_ns, CACHE_SCHEMA_VERSION, state, dt.datetime.now(dt.timezone.utc).isoformat(),
            ))
            self.db.commit()
            return True
        except sqlite3.Error:
            self.db.rollback()
            return False

    def analyzer(self, path: str) -> IncrementalSessionAnalyzer | CodexSessionAnalyzer:
        analyzer = self.load(path)
        if analyzer is None:
            analyzer = create_analyzer(path)
        else:
            analyzer.poll()
        self.save(analyzer)
        return analyzer


def timestamp_hm(value: str | None) -> str:
    if value and "T" in value:
        return value.split("T", 1)[1][:5]
    return "     "


def live_feed(analysis: Analysis) -> list[tuple[int, str]]:
    """Human-readable execution events mapped back to their prompt."""
    items: list[tuple[int, str]] = []
    session_is_live = time.time() - os.path.getmtime(analysis.path) < 2
    index_width = max((len(str(prompt.index)) for prompt in analysis.prompts), default=1)
    for prompt in analysis.prompts:
        clock = timestamp_hm(prompt.timestamp)
        quoted_prompt = json.dumps(prompt.prompt, ensure_ascii=False)
        items.append((prompt.index, f"{prompt.index:>{index_width}} | {clock} · {quoted_prompt}"))
        for event in prompt.events:
            items.append((prompt.index, f"       {event}"))
        usage = prompt.total_usage
        if prompt is analysis.prompts[-1]:
            state = "running" if session_is_live else "last observed"
        else:
            state = "completed"
        items.append((prompt.index, (
            f"       {state} · {fmt_tokens(latest_context_size(prompt))} context "
            f"({latest_context_usage(prompt).cache_hit_rate:.0f}% cached) · "
            f"{fmt_tokens(usage.output)} out · "
            f"{usage.requests} thinking rounds"
        )))
    return items


def history_feed(analysis: Analysis) -> list[tuple[int, str]]:
    items: list[tuple[int, str]] = []
    index_width = max((len(str(prompt.index)) for prompt in analysis.prompts), default=1)
    for prompt in reversed(analysis.prompts):
        usage = prompt.total_usage
        quoted_prompt = json.dumps(prompt.prompt, ensure_ascii=False)
        text = f"{prompt.index:>{index_width}} | {timestamp_hm(prompt.timestamp)} · {quoted_prompt}"
        items.append((prompt.index, (
            f"{text}  ·  {fmt_tokens(latest_context_size(prompt))} context "
            f"({latest_context_usage(prompt).cache_hit_rate:.0f}% cached) · "
            f"{fmt_tokens(usage.output)} out · "
            f"{usage.requests} thinking rounds"
        )))
    return items


DETAIL_SECTIONS = ("Actors", "Timeline", "Files", "Requests", "Context attribution", "Usage observation")


def context_attribution(prompt: PromptTurn) -> list[tuple[str, int]]:
    """Locally attribute observed evidence, reconciled to the exact provider context total."""
    total = latest_context_size(prompt)
    categories = (
        ("System", ("system", "instructions")),
        ("Skills", ("skills",)),
        ("Instructions", ("agents",)),
        ("Repo", ("repository",)),
        ("Conversation", ("conversation",)),
        ("Tool output", ("tool output", "worker results")),
    )
    weights = [sum(prompt.evidence_chars.get(key, 0) for key in keys) for _, keys in categories]
    evidence_total = sum(weights)
    if not evidence_total:
        return []
    values = [round(total * weight / evidence_total) for weight in weights]
    if values:
        target = max(range(len(weights)), key=weights.__getitem__)
        values[target] += total - sum(values)
    allocated = [
        (categories[index][0], value)
        for index, value in enumerate(values)
        if weights[index] > 0 and value > 0
    ]
    return allocated


def main_actor_name(prompt: PromptTurn) -> str:
    model = prompt.main.primary_model.lower()
    return "Codex" if model.startswith(("gpt-", "codex")) else "Claude"


def latest_context_size(prompt: PromptTurn) -> int:
    return latest_context_usage(prompt).context_total


def latest_context_usage(prompt: PromptTurn) -> Usage:
    return prompt.requests[-1].usage if prompt.requests else prompt.total_usage


def prompt_overview_lines(prompt: PromptTurn, width: int) -> list[str]:
    usage = prompt.total_usage
    completed = any(item.label == "Prompt completed" for item in prompt.timeline)
    interrupted = "Interrupted by user" in prompt.events
    status = "interrupted" if interrupted else ("completed" if completed else "last observed")
    timestamps = [parse_iso_timestamp(item.timestamp) for item in prompt.timeline]
    timestamps = [value for value in timestamps if value]
    span = ""
    if timestamps:
        duration = max(0, round((timestamps[-1] - timestamps[0]).total_seconds()))
        span = f" · {timestamp_hm(prompt.timestamp)}–{timestamps[-1].strftime('%H:%M')} · {duration}s"

    actor_parts = []
    if prompt.main.total:
        actor_parts.append(main_actor_name(prompt))
    actor_parts.extend(f"{truncate(actor.label, 30)} ({actor.status})" for actor in prompt.actors[:2])
    if len(prompt.actors) > 2:
        actor_parts.append(f"+{len(prompt.actors) - 2} more")
    actor_summary = " · ".join(actor_parts) or "none observed"

    return [
        f"Prompt {prompt.index}",
        f'“{truncate(prompt.prompt, max(10, width - 4))}”',
        f"{status}{span}",
        "",
        "Overview",
        f"  Actors    {truncate(actor_summary, max(10, width - 14))}",
        (
            f"  I/O       {fmt_tokens(latest_context_size(prompt))} context "
            f"({latest_context_usage(prompt).cache_hit_rate:.0f}% cached) · "
            f"{fmt_tokens(usage.output)} out"
        ),
        (
            f"  Work      {len(prompt.requests)} thinking rounds · "
            f"{len(prompt.timeline)} events · {len(prompt.files)} file ops"
        ),
        "",
        f"Actors                 {len(prompt.actors) + (1 if prompt.main.total else 0)}",
        f"Timeline               {len(prompt.timeline)} events",
        f"Files                  {len(prompt.files)} operations",
        f"Requests               {len(prompt.requests)}",
        f"Context attribution    {fmt_tokens(latest_context_size(prompt))} current",
        f"Usage observation      {len(prompt.usage_observations)} captured",
    ]


def section_line_indices(lines: list[str]) -> list[int]:
    return [
        index for index, line in enumerate(lines)
        if any(line.startswith(section) for section in DETAIL_SECTIONS)
    ]


def request_line_indices(lines: list[str]) -> list[int]:
    return [index for index, line in enumerate(lines) if re.match(r"^\s*\d+\s+\|", line)]


def wrapped_prefixed_lines(
    value: str, width: int, first_prefix: str, continuation_prefix: str,
) -> list[str]:
    available = max(1, width - len(first_prefix) - 1)
    lines: list[str] = []
    first = True
    for physical_line in (value.splitlines() or [""]):
        wrapped = textwrap.wrap(
            physical_line, width=available, replace_whitespace=False,
            drop_whitespace=False, break_long_words=True, break_on_hyphens=False,
        ) or [""]
        for part in wrapped:
            prefix = first_prefix if first else continuation_prefix
            lines.append(prefix + part)
            first = False
    return lines


def output_box_lines(value: str, width: int) -> list[str]:
    indent = "    "
    rule_width = max(8, min(56, width - len(indent) - 1))
    lines = [indent + "┌" + "─" * (rule_width - 1)]
    content = value or "No output captured"
    lines.extend(wrapped_prefixed_lines(content, width, indent + "│ ", indent + "│ "))
    lines.append(indent + "└" + "─" * (rule_width - 1))
    return lines


def request_commands(request: RequestInfo) -> str:
    commands = [
        request.action_details[index]
        for index, action in enumerate(request.actions)
        if action.startswith("Bash")
        and index < len(request.action_details)
        and request.action_details[index]
    ]
    return "\n".join(commands)


def copy_to_clipboard(value: str) -> bool:
    if not value:
        return False
    try:
        subprocess.run(("pbcopy",), input=value, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def request_detail_lines(request: RequestInfo, index: int, width: int) -> list[str]:
    usage = request.usage
    lines = [
        f"Request {index + 1}",
        (
            f"{timestamp_hm(request.timestamp)} · {short_model(request.model, 24)} · "
            f"↓ {fmt_tokens(usage.context_total)} ({usage.cache_hit_rate:.0f}% cached) · "
            f"↑ {fmt_tokens(usage.output)} · {request.stop_reason or '-'}"
        ),
        "", "Actions",
    ]
    if not request.actions:
        return lines + ["  No actions observed"]
    for action_index, action in enumerate(request.actions):
        lines.extend(["", f"{action_index + 1}. {action.split(' · ', 1)[0]}"])
        detail = (
            request.action_details[action_index]
            if action_index < len(request.action_details) and request.action_details[action_index]
            else action
        )
        action_name = action.split(" · ", 1)[0]
        first_prefix = "   $ " if action_name == "Bash" else "     "
        lines.extend(wrapped_prefixed_lines(detail, width, first_prefix, "     "))
        output = (
            request.action_outputs[action_index]
            if action_index < len(request.action_outputs)
            else ""
        )
        lines.extend(["", "   Output"])
        lines.extend(output_box_lines(output, width))
    return lines


def detail_page_lines(prompt: PromptTurn, page: str, width: int) -> list[str]:
    if page == "prompt":
        return prompt_overview_lines(prompt, width)
    if page == "actors":
        lines = ["Actors", ""]
        if prompt.main.total:
            lines.append(
                f"{main_actor_name(prompt)} · {short_model(prompt.main.primary_model, 24)} · "
                f"{prompt.main.requests} requests"
            )
        lines.extend(f"{actor.label} · {actor.status}" for actor in prompt.actors)
        return lines
    if page == "timeline":
        return ["Timeline", ""] + [
            f"{timestamp_hm(item.timestamp)}  {item.label}" for item in prompt.timeline
        ]
    if page == "files":
        lines = ["Files", ""] + [
            f"{timestamp_hm(item.timestamp)}  {item.action:<6}  {item.path}" for item in prompt.files
        ]
        return lines if prompt.files else ["Files", "", "No file activity observed"]
    if page.startswith("request:"):
        try:
            request_index = int(page.split(":", 1)[1])
            request = prompt.requests[request_index]
        except (ValueError, IndexError):
            return ["Request not found"]
        return request_detail_lines(request, request_index, width)
    if page == "requests":
        description = (
            "One row per observed Codex model round (token_count)."
            if main_actor_name(prompt) == "Codex"
            else "One row per unique model request (requestId)."
        )
        lines = ["Requests", description, ""]
        index_width = max(1, len(str(len(prompt.requests))))
        rendered_models = [short_model(request.model, 16) for request in prompt.requests]
        model_width = max((len(model) for model in rendered_models), default=1)
        input_width = max((len(fmt_tokens(request.usage.context_total)) for request in prompt.requests), default=1)
        output_width = max((len(fmt_tokens(request.usage.output)) for request in prompt.requests), default=1)
        for index, request in enumerate(prompt.requests, 1):
            usage = request.usage
            model = rendered_models[index - 1]
            lines.append(
                f"{index:>{index_width}} | {timestamp_hm(request.timestamp)} · "
                f"[ {model:<{model_width}} ] · "
                f"↓ {fmt_tokens(usage.context_total):>{input_width}} ({usage.cache_hit_rate:>3.0f}% cached) · "
                f"↑ {fmt_tokens(usage.output):>{output_width}}"
            )
            for action in request.actions:
                lines.append(f"    → {action}")
        return lines
    if page == "context":
        lines = ["Context (approx.)", ""]
        lines.extend(f"{name:<14}{fmt_tokens(value):>10}" for name, value in context_attribution(prompt))
        lines.extend(["", f"{'Total':<14}{fmt_tokens(latest_context_size(prompt)):>10}"])
        return lines
    if page == "usage":
        if not prompt.usage_observations:
            return ["Usage observation", "", "No observation captured for this prompt."]
        observation = prompt.usage_observations[-1]
        return [
            "Usage observation", "",
            f"Before   {observation.before_time}",
            f"  5h       {observation.before_5h:.1f}%",
            f"  weekly   {observation.before_weekly:.1f}%",
            "",
            f"After    {observation.after_time}",
            f"  5h       {observation.after_5h:.1f}%",
            f"  weekly   {observation.after_weekly:.1f}%",
            "",
            "Delta",
            f"  5h       {observation.delta_5h:+.1f} pp",
            f"  weekly   {observation.delta_weekly:+.1f} pp",
            "",
            "Confidence",
            f"  {observation.confidence}" + (f" · {observation.confidence_reason}" if observation.confidence_reason else ""),
        ]
    return ["Unknown detail page"]


def actor_detail_lines(actor: Actor) -> list[str]:
    lines = [
        actor.label,
        "",
        f"Status       {actor.status}",
        f"Task         {actor.task_id or '-'}",
        f"Started      {actor.started_at or '-'}",
        f"Finished     {actor.finished_at or '-'}",
        f"Exit code    {actor.exit_code if actor.exit_code is not None else '-'}",
        f"Tool use     {actor.tool_use_id or '-'}",
    ]
    if actor.engine:
        lines.extend([
            "",
            "Execution",
            f"  Engine             {actor.engine}",
            f"  Model              {actor.model or 'default'}",
        ])
        if actor.mode:
            lines.append(f"  Mode               {actor.mode}")
        if actor.lane:
            lines.append(f"  Lane               {actor.lane}")
        started = parse_iso_timestamp(actor.started_at)
        finished = parse_iso_timestamp(actor.finished_at)
        if started and finished:
            lines.append(f"  Duration           {(finished - started).total_seconds():.1f}s")
        if actor.verdict:
            lines.extend(["", "Decision", f"  Verdict            {actor.verdict}"])
            if actor.decision:
                lines.append(f"  Question           {actor.decision}")
            if actor.rationale:
                lines.append(f"  Rationale          {actor.rationale}")
    if actor.thread_id or actor.token_usage:
        lines.extend(["", "Response telemetry"])
        if actor.thread_id:
            lines.append(f"  Thread             {actor.thread_id}")
        if actor.duration_seconds is not None:
            lines.append(f"  Duration           {actor.duration_seconds:.1f}s")
        labels = (
            ("input_tokens", "Input"), ("cached_input_tokens", "Cached input"),
            ("output_tokens", "Output"), ("reasoning_output_tokens", "Reasoning"),
            ("total_tokens", "Total"),
        )
        for key, label in labels:
            if key in actor.token_usage:
                lines.append(f"  {label:<18} {fmt_tokens(actor.token_usage[key]):>10}")
        if actor.rate_limits:
            primary = actor.rate_limits.get("primary")
            if isinstance(primary, dict) and primary.get("used_percent") is not None:
                window = primary.get("window_minutes")
                lines.append(f"  Rate limit         {primary['used_percent']}% / {window} min")
    return lines


ACTIVE_PROCESS_STATES = {"starting", "running", "waiting"}


def session_processes(
    analysis: Analysis, prompt_index: int | None = None,
) -> list[tuple[PromptTurn, Actor]]:
    processes = [
        (prompt, actor)
        for prompt in analysis.prompts
        if prompt_index is None or prompt.index == prompt_index
        for actor in prompt.actors
    ]
    return sorted(
        processes,
        key=lambda item: (
            0 if item[1].status in ACTIVE_PROCESS_STATES else 1,
            item[1].started_at or "",
        ),
    )


def actor_file(actor: Actor, value: str | None) -> Path | None:
    if not value or "$" in value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute() and actor.working_dir:
        path = Path(actor.working_dir) / path
    return path


def tail_text(path: Path | None, limit: int = 128_000) -> str:
    if not path:
        return ""
    try:
        with path.open("rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - limit))
            return fh.read().decode(errors="replace")
    except OSError:
        return ""


def actor_log_lines(actor: Actor) -> list[str]:
    text = tail_text(actor_file(actor, actor.events_path))
    chunks = [chunk for chunk in actor.log_chunks if chunk.strip()]
    if text.strip():
        chunks.append(text)
    lines = [line for chunk in chunks for line in chunk.rstrip().splitlines()]
    return lines or ["No log output observed yet."]


def actor_response_lines(actor: Actor) -> list[str]:
    result = tail_text(actor_file(actor, actor.output_path))
    if result.strip():
        return result.rstrip().splitlines()
    response: list[str] = []
    for line in actor_log_lines(actor):
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "assistant" and isinstance(event.get("text"), str):
            response.extend(event["text"].splitlines())
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            response.extend(item["text"].splitlines())
    return response or ["No realtime response observed yet."]


def process_elapsed(actor: Actor) -> str:
    started = parse_iso_timestamp(actor.started_at)
    if not started:
        return "--:--"
    finished = parse_iso_timestamp(actor.finished_at)
    now = dt.datetime.now(dt.timezone.utc)
    seconds = max(0, int(((finished or now) - started).total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def process_clock(timestamp: str | None) -> str:
    parsed = parse_iso_timestamp(timestamp)
    return parsed.astimezone().strftime("%H:%M:%S") if parsed else "--:--:--"


def fit_action_bar(full: str, compact: str, width: int) -> str:
    available = max(1, width - 1)
    value = full if len(full) <= available else compact
    return truncate(value, available).ljust(available)


def file_path_span(text: str) -> tuple[int, int] | None:
    """Locate a rendered tool/file path so it can be visually de-emphasized."""
    marker = " · "
    if marker in text:
        start = text.rfind(marker) + len(marker)
        candidate = text[start:]
        if candidate.startswith(("/", "~/", "logs/", "docs/", "schema/", "tools/", "openspec/")):
            end_marker = candidate.find(" — ")
            end = start + (end_marker if end_marker >= 0 else len(candidate))
            return start, end
    match = re.search(r"^\s*\d{1,2}:\d{2}\s+\w+\s+((?:/|~/)\S.*)$", text)
    if match:
        return match.start(1), match.end(1)
    return None


def action_span(text: str) -> tuple[int, int] | None:
    """Locate a tool action label without coloring its description or path."""
    names = "|".join(ACTION_NAMES)
    match = re.search(rf"\b({names})\b(?=\s*(?:·|\s{{2}}))", text, re.IGNORECASE)
    return (match.start(1), match.end(1)) if match else None


class TTYApp:
    def __init__(self, screen: Any, path: str, cache: ProfilerCache | None = None) -> None:
        self.screen = screen
        self.cache = cache
        self.action_attrs: dict[str, int] = {}
        self.path = path
        self.all_session_paths = find_all_sessions()
        if path not in self.all_session_paths:
            self.all_session_paths.insert(0, path)
        active_provider = session_provider(path)
        self.session_paths = [
            item for item in self.all_session_paths if session_provider(item) == active_provider
        ]
        self.session_index = self.session_paths.index(path)
        self.provider_positions: dict[str, str] = {active_provider: path}
        self.view = 1
        self.mode = "list"
        self.follow = True
        self.new_events = 0
        self.cursor = {1: 0, 2: 0}
        self.offset = {1: 0, 2: 0}
        self.detail_offset = 0
        self.detail_page = "prompt"
        self.detail_cursor = 0
        self.clipboard_notice = ""
        self.process_cursor = 0
        self.process_offset = 0
        self.process_tab = "response"
        self.process_follow = True
        self.selected_prompt = 1
        self.search_query = ""
        self.search_matches: list[int] = []
        self.search_position = -1
        self.last_mtime = -1.0
        self.last_refresh = 0.0
        self.analyzer = cache.analyzer(path) if cache else create_analyzer(path)
        self.analysis = self.analyzer.analysis
        self.last_record_count = self.analysis.record_count
        self.feed: list[tuple[int, str]] = []
        self.history: list[tuple[int, str]] = []
        self.previous_frame: list[tuple[str, int]] = []
        self.status = "ready"

    def refresh_session_catalog(self) -> None:
        current_provider = session_provider(self.path)
        previous_paths = self.session_paths
        all_paths = find_all_sessions()
        candidates = [
            path for path in all_paths if session_provider(path) == current_provider
        ]
        if self.path not in candidates:
            candidates.insert(min(self.session_index, len(candidates)), self.path)
            all_paths.append(self.path)
        self.all_session_paths = all_paths
        self.session_paths = candidates
        self.session_index = candidates.index(self.path)
        if candidates != previous_paths:
            self.previous_frame = []

    def switch_session(self, delta: int) -> None:
        self.refresh_session_catalog()
        index = max(0, min(len(self.session_paths) - 1, self.session_index + delta))
        if index == self.session_index:
            return
        self.session_index = index
        self.path = self.session_paths[index]
        self.provider_positions[session_provider(self.path)] = self.path
        self.analyzer = self.cache.analyzer(self.path) if self.cache else create_analyzer(self.path)
        self.analysis = self.analyzer.analysis
        self.last_record_count = self.analysis.record_count
        self.mode, self.follow, self.new_events = "list", True, 0
        self.cursor, self.offset = {1: 0, 2: 0}, {1: 0, 2: 0}
        self.last_mtime = -1.0
        self.refresh(force=True)
        if self.search_query:
            self.update_search()

    def session_navigation_availability(self) -> tuple[bool, bool]:
        return self.session_index > 0, self.session_index < len(self.session_paths) - 1

    def session_header(self, width: int) -> str:
        provider = self.analysis.provider.title()
        prefix = f"< · {provider} · "
        suffix = f" · {self.status} · >"
        stem_width = max(1, width - len(prefix) - len(suffix))
        return truncate_layout(prefix + truncate(Path(self.path).stem, stem_width) + suffix, width)

    def select_provider(self, target_provider: str) -> None:
        current_provider = session_provider(self.path)
        if target_provider == current_provider:
            return
        candidates = [
            path for path in self.all_session_paths
            if session_provider(path) == target_provider
        ]
        if not candidates:
            self.status = f"no {target_provider} sessions"
            return
        self.provider_positions[current_provider] = self.path
        target = self.provider_positions.get(target_provider, candidates[0])
        if target not in candidates:
            target = candidates[0]
        self.session_paths = candidates
        self.session_index = candidates.index(target)
        self.path = target
        self.provider_positions[target_provider] = target
        self.analyzer = self.cache.analyzer(self.path) if self.cache else create_analyzer(self.path)
        self.analysis = self.analyzer.analysis
        self.last_record_count = self.analysis.record_count
        self.mode, self.follow, self.new_events = "list", True, 0
        self.cursor, self.offset = {1: 0, 2: 0}, {1: 0, 2: 0}
        self.last_mtime = -1.0
        self.refresh(force=True)
        if self.search_query:
            self.update_search()

    def refresh(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.last_refresh < REFRESH_INTERVAL:
            return
        self.last_refresh = now
        try:
            mtime = os.path.getmtime(self.path)
            if force or mtime != self.last_mtime:
                old_record_count = self.last_record_count
                if self.analyzer.path != self.path:
                    self.analyzer = self.cache.analyzer(self.path) if self.cache else create_analyzer(self.path)
                else:
                    changed = self.analyzer.poll()
                    if changed and self.cache:
                        self.cache.save(self.analyzer)
                self.analysis = self.analyzer.analysis
                self.feed = live_feed(self.analysis)
                self.history = history_feed(self.analysis)
                if self.search_query:
                    self.update_search()
                added = max(0, self.analysis.record_count - old_record_count)
                if self.follow:
                    self.cursor[1] = self.prompt_anchor(len(self.analysis.prompts))
                    self.new_events = 0
                elif old_record_count:
                    self.new_events += added
                self.cursor[2] = min(self.cursor[2], max(0, len(self.history) - 1))
                self.last_record_count = self.analysis.record_count
                self.last_mtime = mtime
                self.status = dt.datetime.now().strftime("%H:%M:%S")
        except OSError as exc:
            self.status = f"error: {exc}"

    def current_items(self) -> list[tuple[int, str]]:
        return self.feed if self.view == 1 else self.history

    def prompt_anchor(self, prompt_index: int) -> int:
        """Return the selectable heading row for a prompt in the live feed."""
        for index, (item_prompt, text) in enumerate(self.feed):
            if item_prompt == prompt_index and text.lstrip().startswith(tuple("0123456789")):
                return index
        return next(
            (index for index, (item_prompt, _) in enumerate(self.feed) if item_prompt == prompt_index),
            0,
        )

    def move(self, delta: int) -> None:
        items = self.current_items()
        if not items:
            return
        if self.view == 1:
            prompt_ids = [prompt.index for prompt in self.analysis.prompts]
            current_prompt = items[self.cursor[1]][0]
            try:
                position = prompt_ids.index(current_prompt)
            except ValueError:
                position = 0
            position = max(0, min(len(prompt_ids) - 1, position + delta))
            self.cursor[1] = self.prompt_anchor(prompt_ids[position])
        else:
            self.cursor[2] = max(0, min(len(items) - 1, self.cursor[2] + delta))
        if self.view == 1 and delta < 0:
            self.follow = False

    def follow_latest(self) -> None:
        self.follow, self.new_events = True, 0
        self.cursor[1] = self.prompt_anchor(len(self.analysis.prompts))

    def inspect(self) -> None:
        items = self.current_items()
        if items:
            index = max(0, min(self.cursor[self.view], len(items) - 1))
            self.cursor[self.view] = index
            self.selected_prompt = items[index][0]
            self.detail_offset = 0
            self.detail_page = "prompt"
            self.detail_cursor = 0
            self.mode = "detail"

    def selected_prompt_turn(self) -> PromptTurn | None:
        return next((p for p in self.analysis.prompts if p.index == self.selected_prompt), None)

    def selected_process(self) -> tuple[PromptTurn, Actor] | None:
        processes = session_processes(self.analysis, self.selected_prompt)
        if not processes:
            return None
        self.process_cursor = max(0, min(self.process_cursor, len(processes) - 1))
        return processes[self.process_cursor]

    def open_detail_selection(self) -> None:
        prompt = self.selected_prompt_turn()
        if not prompt:
            return
        if self.detail_page == "prompt":
            self.detail_page = ("actors", "timeline", "files", "requests", "context", "usage")[self.detail_cursor]
            self.detail_cursor = 0
            self.detail_offset = 0
        elif self.detail_page == "actors" and (prompt.actors or prompt.main.total):
            if prompt.main.total and self.detail_cursor == 0:
                self.detail_page = "actor:main"
                self.detail_offset = 0
                return
            actor_index = self.detail_cursor - (1 if prompt.main.total else 0)
            if actor_index >= 0:
                self.detail_page = f"actor:{actor_index}"
                self.detail_offset = 0
        elif self.detail_page == "requests" and prompt.requests:
            self.detail_cursor = min(self.detail_cursor, len(prompt.requests) - 1)
            self.detail_page = f"request:{self.detail_cursor}"
            self.detail_offset = 0
            self.clipboard_notice = ""

    def back_detail(self) -> None:
        if self.detail_page.startswith("actor:"):
            self.detail_page = "actors"
        elif self.detail_page.startswith("request:"):
            self.detail_page = "requests"
        elif self.detail_page != "prompt":
            self.detail_page = "prompt"
            self.detail_cursor = 0
        else:
            self.mode = "list"

    def update_search(self) -> None:
        query = self.search_query.casefold()
        if self.view == 1:
            matching_prompts = {
                prompt_index for prompt_index, text in self.feed
                if query and query in text.casefold()
            }
            self.search_matches = [
                self.prompt_anchor(prompt.index) for prompt in self.analysis.prompts
                if prompt.index in matching_prompts
            ]
        else:
            self.search_matches = [
                index for index, (_, text) in enumerate(self.history)
                if query and query in text.casefold()
            ]
        self.search_position = 0 if self.search_matches else -1

    def search_next(self, direction: int) -> None:
        if not self.search_matches:
            return
        items = self.current_items()
        self.search_matches = [index for index in self.search_matches if 0 <= index < len(items)]
        if not self.search_matches:
            self.search_position = -1
            return
        self.search_position = (self.search_position + direction) % len(self.search_matches)
        match = self.search_matches[self.search_position]
        if self.view == 1:
            prompt_index = self.feed[match][0]
            self.cursor[1] = self.prompt_anchor(prompt_index)
            self.follow = False
        else:
            self.cursor[2] = min(match, max(0, len(self.history) - 1))

    def handle_key(self, key: int) -> bool:
        if self.mode == "search":
            if key == 27:
                self.mode = "list"
            elif key in (10, 13, curses.KEY_ENTER):
                self.update_search()
                self.mode = "list"
                if self.search_matches:
                    self.search_position = -1
                    self.search_next(1)
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                self.search_query = self.search_query[:-1]
            elif 32 <= key <= 126:
                self.search_query += chr(key)
            return True
        if self.mode == "help":
            if key in (27, curses.KEY_LEFT, ord("?"), ord("q")):
                self.mode = "list"
            return True
        if self.mode in {"processes", "process_detail"}:
            if key == ord("q"):
                return False
            if key == ord("p"):
                self.mode = "list"
            elif key in (27, curses.KEY_LEFT):
                self.mode = "processes" if self.mode == "process_detail" else "list"
            elif self.mode == "processes":
                count = len(session_processes(self.analysis, self.selected_prompt))
                if key == curses.KEY_DOWN:
                    self.process_cursor = min(max(0, count - 1), self.process_cursor + 1)
                elif key == curses.KEY_UP:
                    self.process_cursor = max(0, self.process_cursor - 1)
                elif key in (10, 13, curses.KEY_ENTER, curses.KEY_RIGHT) and count:
                    self.mode, self.process_tab, self.process_offset = "process_detail", "response", 0
            else:
                if key == 9:
                    tabs = ("response", "logs", "metadata")
                    self.process_tab = tabs[(tabs.index(self.process_tab) + 1) % len(tabs)]
                    self.process_offset = 0
                elif key == ord("f"):
                    self.process_follow = not self.process_follow
                elif key == curses.KEY_DOWN:
                    self.process_follow = False
                    self.process_offset += 1
                elif key == curses.KEY_UP:
                    self.process_follow = False
                    self.process_offset = max(0, self.process_offset - 1)
                elif key == curses.KEY_HOME:
                    self.process_follow, self.process_offset = False, 0
                elif key == curses.KEY_END:
                    self.process_follow = True
            return True
        if key == ord("q"):
            return False
        if key == ord("z"):
            self.select_provider("claude")
            return True
        if key == ord("p") and self.mode == "list":
            items = self.current_items()
            if items:
                index = max(0, min(self.cursor[self.view], len(items) - 1))
                self.selected_prompt = items[index][0]
            self.mode, self.process_cursor, self.process_offset = "processes", 0, 0
            return True
        if key == ord("x"):
            self.select_provider("codex")
            return True
        if key in (27, curses.KEY_LEFT):
            if self.mode == "detail":
                self.back_detail()
            return True
        if self.mode == "detail":
            prompt = self.selected_prompt_turn()
            if key == ord("y") and prompt and self.detail_page.startswith("request:"):
                try:
                    request_index = int(self.detail_page.split(":", 1)[1])
                    command_text = request_commands(prompt.requests[request_index])
                except (ValueError, IndexError):
                    command_text = ""
                self.clipboard_notice = "copied" if copy_to_clipboard(command_text) else "nothing to copy"
                return True
            if self.detail_page == "prompt":
                if key == curses.KEY_DOWN: self.detail_cursor = min(5, self.detail_cursor + 1)
                elif key == curses.KEY_UP: self.detail_cursor = max(0, self.detail_cursor - 1)
            elif self.detail_page == "actors" and prompt:
                actor_count = len(prompt.actors) + (1 if prompt.main.total else 0)
                if key == curses.KEY_DOWN: self.detail_cursor = min(max(0, actor_count - 1), self.detail_cursor + 1)
                elif key == curses.KEY_UP: self.detail_cursor = max(0, self.detail_cursor - 1)
            elif self.detail_page == "requests" and prompt:
                if key == curses.KEY_DOWN: self.detail_cursor = min(max(0, len(prompt.requests) - 1), self.detail_cursor + 1)
                elif key == curses.KEY_UP: self.detail_cursor = max(0, self.detail_cursor - 1)
            else:
                if key == curses.KEY_DOWN: self.detail_offset += 1
                elif key == curses.KEY_UP: self.detail_offset = max(0, self.detail_offset - 1)
            if key in (10, 13, curses.KEY_ENTER, curses.KEY_RIGHT):
                self.open_detail_selection()
            return True
        if key == ord("/"):
            self.search_query = ""
            self.mode = "search"
        elif key == ord("?"):
            self.mode = "help"
        elif key == ord("n"):
            self.search_next(1)
        elif key == ord("N"):
            self.search_next(-1)
        elif key == ord("1"):
            self.view = 1
            if self.search_query:
                self.update_search()
        elif key == ord("2"):
            self.view, self.follow = 2, False
            if self.search_query:
                self.update_search()
        elif key == ord("["):
            self.switch_session(-1)
        elif key == ord("]"):
            self.switch_session(1)
        elif key == curses.KEY_DOWN:
            if self.view == 1 and self.cursor[1] == self.prompt_anchor(len(self.analysis.prompts)):
                self.follow_latest()
            else:
                self.move(1)
        elif key == curses.KEY_UP:
            self.move(-1)
        elif key == curses.KEY_NPAGE:
            self.move(max(1, self.screen.getmaxyx()[0] - 5))
        elif key == curses.KEY_PPAGE:
            self.move(-max(1, self.screen.getmaxyx()[0] - 5))
        elif key == curses.KEY_HOME:
            if self.view == 1 and self.analysis.prompts:
                self.cursor[1] = self.prompt_anchor(self.analysis.prompts[0].index)
                self.follow = False
            else:
                self.cursor[2] = 0
        elif key == curses.KEY_END and self.view == 1:
            self.follow_latest()
        elif key in (10, 13, curses.KEY_ENTER, curses.KEY_RIGHT):
            self.inspect()
        elif key == ord("r"):
            self.refresh(force=True)
        elif key == curses.KEY_RESIZE:
            self.previous_frame = []
            self.screen.clear()
        return True

    def frame(self) -> list[tuple[str, int]]:
        height, width = self.screen.getmaxyx()
        body_height = max(1, height - 3)
        active_live = "[1] Live" if self.view == 1 else " 1  Live"
        active_history = "[2] History" if self.view == 2 else " 2  History"
        source_tabs = "[z] Claude   x  Codex" if self.analysis.provider == "claude" else " z  Claude  [x] Codex"
        header_left = f" Execution Profiler   {active_live}  {active_history}"
        gap = max(2, width - 1 - len(header_left) - len(source_tabs))
        header = truncate_layout(header_left + " " * gap + source_tabs, width - 1)
        rows: list[tuple[str, int]] = [(header, curses.A_BOLD)]
        rows.append((self.session_header(width - 1), 0))

        if self.mode == "processes":
            processes = session_processes(self.analysis, self.selected_prompt)
            active = sum(actor.status in ACTIVE_PROCESS_STATES for _, actor in processes)
            rows.append((f" Prompt {self.selected_prompt} background agents · {active} active · {len(processes) - active} finished", curses.A_BOLD))
            rows.append(("", 0))
            rendered: list[tuple[int, str]] = []
            previous_group = ""
            for index, (prompt, actor) in enumerate(processes):
                group = "ACTIVE" if actor.status in ACTIVE_PROCESS_STATES else "FINISHED"
                if group != previous_group:
                    rendered.append((-1, f"{group:<8}  ENGINE   STATUS      STARTED   FINISHED  ELAPSED"))
                    previous_group = group
                engine = (actor.engine or "process")[:8]
                rendered.append((index, (
                    f"{engine:<8} {actor.status:<10} {process_clock(actor.started_at)} "
                    f"{process_clock(actor.finished_at)} {process_elapsed(actor):>8}  "
                    f"{actor.label}  · prompt {prompt.index}"
                )))
            visible_height = max(1, body_height - 2)
            selected_row = next((i for i, (index, _) in enumerate(rendered) if index == self.process_cursor), 0)
            if selected_row < self.process_offset:
                self.process_offset = selected_row
            elif selected_row >= self.process_offset + visible_height:
                self.process_offset = selected_row - visible_height + 1
            for index, line in rendered[self.process_offset:self.process_offset + visible_height]:
                rows.append((
                    ("> " if index >= 0 else "  ") + truncate_layout(line, width - 3),
                    curses.A_REVERSE if index == self.process_cursor else (curses.A_BOLD if index < 0 else 0),
                ))
            if not processes:
                rows.append((f"  No spawned background agents observed for prompt {self.selected_prompt}.", 0))
            footer = fit_action_bar(
                " PROCESSES │ ↑/↓ select │ → detail │ p/← back │ q quit",
                " PROCESSES │ ↑/↓ │ → detail │ p back",
                width,
            )
        elif self.mode == "process_detail":
            selected = self.selected_process()
            if selected:
                prompt, actor = selected
                tabs = "  ".join(f"[{name}]" if name == self.process_tab else name for name in ("response", "logs", "metadata"))
                rows.append((truncate_layout(f" {actor.engine or 'process'} · {actor.label} · {actor.status} · {process_elapsed(actor)}", width - 1), curses.A_BOLD))
                rows.append((f" {tabs}", 0))
                if self.process_tab == "response":
                    lines = actor_response_lines(actor)
                elif self.process_tab == "logs":
                    lines = actor_log_lines(actor)
                else:
                    lines = actor_detail_lines(actor) + ["", f"Prompt       {prompt.index}", f"Output       {actor.output_path or '-'}", f"Events       {actor.events_path or '-'}"]
                visible_height = max(1, body_height - 2)
                max_offset = max(0, len(lines) - visible_height)
                if self.process_follow and self.process_tab in {"response", "logs"}:
                    self.process_offset = max_offset
                else:
                    self.process_offset = min(self.process_offset, max_offset)
                for line in lines[self.process_offset:self.process_offset + visible_height]:
                    rows.append(("  " + truncate_layout(line, width - 3), 0))
            else:
                rows.append(("  Process not found", 0))
            follow = "follow" if self.process_follow else "paused"
            footer = fit_action_bar(
                f" PROCESS DETAIL │ Tab view │ f {follow} │ ↑/↓ scroll │ ← back",
                f" DETAIL │ Tab │ f {follow} │ ← back",
                width,
            )
        elif self.mode == "detail":
            prompt = self.selected_prompt_turn()
            if prompt and self.detail_page.startswith("actor:"):
                actor_key = self.detail_page.split(":", 1)[1]
                if actor_key == "main":
                    is_last = prompt is self.analysis.prompts[-1]
                    is_live = time.time() - os.path.getmtime(self.analysis.path) < 2
                    main_status = "running" if is_last and is_live else ("last observed" if is_last else "completed")
                    lines = [
                        f"{main_actor_name(prompt)} · {short_model(prompt.main.primary_model, 28)}", "",
                        f"Status       {main_status}", f"Requests     {prompt.main.requests}",
                        f"Context      {fmt_tokens(prompt.main.context_total)}",
                        f"Output       {fmt_tokens(prompt.main.output)}",
                    ]
                else:
                    actor_index = int(actor_key)
                    lines = actor_detail_lines(prompt.actors[actor_index]) if actor_index < len(prompt.actors) else ["Actor not found"]
            else:
                lines = detail_page_lines(prompt, self.detail_page, width - 2) if prompt else ["Prompt not found"]
            prompt_sections = section_line_indices(lines) if self.detail_page == "prompt" else []
            request_lines = request_line_indices(lines) if self.detail_page == "requests" else []
            max_offset = max(0, len(lines) - body_height)
            self.detail_offset = min(self.detail_offset, max_offset)
            selected_line = None
            if self.detail_page == "prompt" and prompt_sections:
                selected_line = prompt_sections[min(self.detail_cursor, len(prompt_sections) - 1)]
            elif self.detail_page == "actors":
                selected_line = self.detail_cursor + 2
            elif self.detail_page == "requests" and request_lines:
                selected_line = request_lines[min(self.detail_cursor, len(request_lines) - 1)]
            if selected_line is not None:
                if selected_line < self.detail_offset:
                    self.detail_offset = selected_line
                elif selected_line >= self.detail_offset + body_height:
                    self.detail_offset = selected_line - body_height + 1
            for line_index, line in enumerate(lines[self.detail_offset:self.detail_offset + body_height], start=self.detail_offset):
                selectable = False
                if self.detail_page == "prompt" and line_index in prompt_sections:
                    selectable = prompt_sections.index(line_index) == self.detail_cursor
                elif self.detail_page == "actors" and line_index >= 2:
                    selectable = line_index - 2 == self.detail_cursor
                elif self.detail_page == "requests" and line_index in request_lines:
                    selectable = request_lines.index(line_index) == self.detail_cursor
                attr = curses.A_REVERSE if selectable else (curses.A_BOLD if line_index == 0 else 0)
                enterable = (
                    (self.detail_page == "prompt" and line_index in prompt_sections)
                    or (self.detail_page == "actors" and line_index >= 2)
                    or (self.detail_page == "requests" and line_index in request_lines)
                )
                prefix = "> " if enterable else "  "
                rows.append((prefix + truncate_layout(line, width - len(prefix) - 1), attr))
            if self.detail_page.startswith("request:"):
                notice = f" │ {self.clipboard_notice}" if self.clipboard_notice else ""
                footer = fit_action_bar(
                    f" REQUEST DETAIL │ ↑/↓ scroll │ y copy command{notice} │ ← back │ q quit",
                    f" DETAIL │ ↑/↓ │ y copy{notice} │ ← back",
                    width,
                )
            else:
                footer = fit_action_bar(
                    " INSPECT │ ↑/↓ select/scroll │ → open │ ← back │ q quit",
                    " INSPECT │ ↑/↓ │ → open │ ← back │ q",
                    width,
                )
        elif self.mode == "help":
            lines = [
                "Help",
                "",
                "1 / 2       Live / History",
                "↑ / ↓       Previous / next prompt",
                "Home / End  First prompt / follow latest",
                "→ / Enter   Open selected item",
                "← / Esc     Back one level",
                "[           Previous session",
                "]           Next session",
                "z           Claude sessions",
                "x           Codex sessions",
                "p           Spawned background agents for this session",
                "/           Search current view",
                "n / N       Next / previous search result",
                "q           Quit",
            ]
            for line in lines[:body_height]:
                rows.append((" " + line, curses.A_BOLD if line == "Help" else 0))
            footer = fit_action_bar(" HELP │ esc back │ ? close │ q back", " HELP │ esc/? back", width)
        elif self.mode == "search":
            rows.append((" Search the current view", curses.A_BOLD))
            rows.append(("", 0))
            rows.append((f" /{self.search_query}", 0))
            footer = fit_action_bar(" SEARCH │ type query │ ↵ apply │ esc cancel", " SEARCH │ ↵ apply │ esc", width)
        else:
            items = self.current_items()
            cursor = self.cursor[self.view]
            if self.view == 1 and self.follow:
                self.offset[1] = max(0, len(items) - body_height)
            else:
                if cursor < self.offset[self.view]: self.offset[self.view] = cursor
                if cursor >= self.offset[self.view] + body_height: self.offset[self.view] = cursor - body_height + 1
            start = self.offset[self.view]
            for index, (prompt_index, text) in enumerate(items[start:start + body_height], start=start):
                enterable = self.view == 2 or index == self.prompt_anchor(prompt_index)
                prefix = "> " if enterable else "  "
                rows.append((
                    prefix + truncate(text, width - len(prefix) - 1),
                    curses.A_REVERSE if index == cursor else 0,
                ))
            if self.view == 1 and self.follow:
                footer = fit_action_bar(
                    " FOLLOW │ ↑/↓ prompt │ → open │ p processes │ / search │ ? help │ q quit",
                    " FOLLOW │ ↑/↓ │ → open │ p processes │ / │ ? │ q",
                    width,
                )
            elif self.view == 1:
                updates = f"{self.new_events} updates │ " if self.new_events else ""
                footer = fit_action_bar(
                    f" PAUSED │ {updates}End follow │ ↑/↓ prompt │ → open │ p processes │ / search │ ? help │ q quit",
                    f" PAUSED │ {updates}End follow │ → open │ p processes │ / │ ? │ q",
                    width,
                )
            else:
                footer = fit_action_bar(
                    " HISTORY │ ↑/↓ prompt │ → open │ p processes │ / search │ ? help │ q quit",
                    " HISTORY │ ↑/↓ │ → open │ p processes │ / │ ? │ q",
                    width,
                )

        while len(rows) < height - 1:
            rows.append(("", 0))
        rows = rows[:height - 1]
        rows.append((footer, curses.A_REVERSE))
        return rows

    def draw(self) -> None:
        height, width = self.screen.getmaxyx()
        frame = self.frame()
        for row in range(height):
            current = frame[row] if row < len(frame) else ("", 0)
            previous = self.previous_frame[row] if row < len(self.previous_frame) else None
            if current == previous:
                continue
            try:
                self.screen.move(row, 0)
                self.screen.clrtoeol()
                self.screen.addnstr(row, 0, current[0], max(1, width - 1), current[1])
                if row == 1:
                    previous_available, next_available = self.session_navigation_availability()
                    if not previous_available:
                        self.screen.addnstr(row, 0, "<", 1, current[1] | curses.A_DIM)
                    next_position = current[0].rfind(">")
                    if not next_available and next_position >= 0:
                        self.screen.addnstr(
                            row, next_position, ">", 1, current[1] | curses.A_DIM,
                        )
                tool_span = action_span(current[0])
                if tool_span:
                    start, end = tool_span
                    action_attr = self.action_attrs.get(current[0][start:end].lower(), 0)
                    if action_attr:
                        self.screen.addnstr(
                            row, start, current[0][start:end],
                            min(end - start, max(1, width - start - 1)),
                            current[1] | action_attr,
                        )
                path_span = file_path_span(current[0])
                if path_span:
                    start, end = path_span
                    self.screen.addnstr(
                        row, start, current[0][start:end],
                        min(end - start, max(1, width - start - 1)),
                        current[1] | curses.A_DIM,
                    )
            except curses.error:
                pass
        self.previous_frame = frame
        self.screen.noutrefresh()
        curses.doupdate()

    def run(self) -> None:
        curses.set_escdelay(ESCAPE_DELAY_MS)
        curses.curs_set(0)
        if curses.has_colors():
            try:
                curses.start_color()
                curses.use_default_colors()
                for action, (pair, foreground, background) in ACTION_PALETTES.items():
                    curses.init_pair(pair, foreground, background)
                    self.action_attrs[action] = curses.color_pair(pair)
            except curses.error:
                self.action_attrs = {}
        self.screen.timeout(100)
        self.screen.keypad(True)
        self.refresh(force=True)
        while True:
            self.refresh()
            self.draw()
            key = self.screen.getch()
            if key != -1 and not self.handle_key(key):
                break


def print_session_list(limit: int) -> None:
    for ordering_time, path in find_all_session_entries()[:limit]:
        modified = dt.datetime.fromtimestamp(ordering_time).strftime("%Y-%m-%d %H:%M")
        print(
            f"{modified}  {session_provider(path):<6}  "
            f"{os.path.getsize(path) / 1024 / 1024:7.1f} MiB  {path}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live TTY execution profiler for Claude Code and Codex JSONL sessions.")
    parser.add_argument("target", nargs="?", help="Session JSONL or project directory")
    parser.add_argument("--list", action="store_true", help="List recent sessions")
    parser.add_argument("--list-limit", type=int, default=20)
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.list:
        print_session_list(args.list_limit)
        return

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit("TTY required. Run this directly in a terminal.")

    target = resolve_target(args.target)
    try:
        cache = ProfilerCache()
    except (OSError, sqlite3.Error):
        cache = None
    curses.wrapper(lambda screen: TTYApp(screen, target, cache).run())


if __name__ == "__main__":
    main()
