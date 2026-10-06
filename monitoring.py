#!/usr/bin/env python3
"""
monitoring.py — live execution profiler for Claude Code and Codex sessions.

Views
  [1] Live     prompt-centric execution tree, auto-refreshed
  [2] History  newest prompts first
  [o] Overall  shallow usage summary across sessions active in the last 90 days

Keys
  1 / 2       switch view
  [           previous session
  ]           next session
  z x c v ... switch between the profiles discovered on this machine
  o / O       Overall consumption page
  p           background processes for the current session; on the Overall
              page, p/P instead exports a self-contained shareable HTML report
  d / D       detach the current view to a browser dashboard served on
              127.0.0.1 only (any page) / stop that dashboard
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
import copy
import curses
import datetime as dt
import glob
import html
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
import unicodedata
import urllib.error

from collections import Counter
from dataclasses import fields, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable


REFRESH_INTERVAL = 0.5
CACHE_SCHEMA_VERSION = 15
VERSION = "0.4.0"
RELEASE_REPOSITORY = "nutthaphonCh/agents-monitor"
LATEST_RELEASE_API = f"https://api.github.com/repos/{RELEASE_REPOSITORY}/releases/latest"
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


from agent_monitor.models import (
    MODEL_TYPES, OVERALL_WINDOW_DAYS, PROFILE_HOTKEYS, Actor, Analysis,
    ConsumptionConfig, DayUsage, FileActivity, OverallReport, ProjectUsage,
    PromptTurn, RequestInfo, SessionProfile, SessionUsage, SubSession,
    TimelineItem, Usage, UsageObservation, configured_weight,
)

CONSUMPTION_CONFIG = ConsumptionConfig()


def default_projects_dir() -> Path:
    return Path(os.path.expanduser("~/.claude/projects"))


def default_codex_sessions_dir() -> Path:
    return Path(os.path.expanduser("~/.codex/sessions"))


def default_codex_state_db() -> Path:
    return Path(os.path.expanduser("~/.codex/state_5.sqlite"))


def codex_profile_label(home: Path) -> str:
    if home == Path.home() / ".codex":
        return "Codex"
    suffix = home.name.removeprefix(".codex-")
    if suffix == "the-second":
        return "2nd"
    return suffix.replace("-", " ").title() or home.name


def discover_session_profiles() -> list[SessionProfile]:
    """Discover host-local profiles in stable shortcut order."""
    profiles: list[SessionProfile] = []
    claude_root = default_projects_dir()
    if claude_root.is_dir():
        profiles.append(SessionProfile("claude", "Claude", "claude", claude_root))

    conventional_home = Path.home() / ".codex"
    default_home = default_codex_sessions_dir().parent
    homes = [default_home]
    # Keeping the default roots injectable makes discovery deterministic for
    # embedders and tests. Host-wide isolated-profile discovery applies only
    # when the conventional default root is in use.
    if default_home == conventional_home:
        homes.extend(sorted(path for path in Path.home().glob(".codex-*") if path.is_dir()))
        configured_home = os.environ.get("CODEX_HOME", "").strip()
        if configured_home:
            homes.append(Path(os.path.expanduser(configured_home)))

    seen: set[str] = set()
    for home in homes:
        resolved = str(home.resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        sessions_dir = home / "sessions"
        if not sessions_dir.is_dir():
            continue
        profile_id = "codex" if home == default_home else f"codex:{resolved}"
        profiles.append(SessionProfile(
            profile_id, codex_profile_label(home), "codex", sessions_dir,
        ))
    return profiles


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


def codex_subagent_metadata(path: str) -> dict[str, Any] | None:
    """Return normalized parent/link metadata for a Codex sub-agent rollout."""
    try:
        with Path(path).open(encoding="utf-8") as session_file:
            record = json.loads(session_file.readline())
    except (OSError, json.JSONDecodeError):
        return None
    payload = record.get("payload") if isinstance(record, dict) else None
    if not isinstance(payload, dict) or payload.get("thread_source") != "subagent":
        return None
    source = payload.get("source")
    subagent = source.get("subagent") if isinstance(source, dict) else None
    spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
    if not isinstance(spawn, dict):
        spawn = {}
    thread_id = payload.get("id")
    parent_id = spawn.get("parent_thread_id") or payload.get("session_id")
    if not isinstance(thread_id, str) or not thread_id or not isinstance(parent_id, str) or not parent_id:
        return None
    return {
        "thread_id": thread_id,
        "parent_thread_id": parent_id,
        "agent_path": str(spawn.get("agent_path") or ""),
        "agent_nickname": str(spawn.get("agent_nickname") or ""),
        "depth": int(spawn.get("depth") or 0),
    }


def fallback_codex_chats(sessions_dir: Path | None = None) -> list[tuple[float, str]]:
    """Discover one latest user-facing rollout per chat without the state DB."""
    root = sessions_dir or default_codex_sessions_dir()
    if not root.exists():
        return []
    chats: dict[str, tuple[float, str]] = {}
    for path in glob.glob(str(root / "**" / "*.jsonl"), recursive=True):
        metadata = codex_session_metadata(path)
        if metadata is None:
            chat_id, source = path, ""
        else:
            chat_id, source = metadata
        if source == "subagent" or codex_subagent_metadata(path):
            continue
        modified = os.path.getmtime(path)
        current = chats.get(chat_id)
        if current is None or modified > current[0]:
            chats[chat_id] = (modified, path)
    return list(chats.values())


def codex_chats(profile: SessionProfile | None = None) -> list[tuple[float, str]]:
    """Read Codex's chat ordering and current rollout path from local state."""
    state_db = (
        default_codex_state_db()
        if profile is None or profile.id == "codex"
        else profile.sessions_dir.parent / "state_5.sqlite"
    )
    try:
        connection = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                """
                SELECT rollout_path, recency_at_ms, recency_at,
                       updated_at_ms, updated_at, created_at_ms, created_at
                FROM threads
                WHERE archived = 0
                  AND COALESCE(thread_source, '') != 'subagent'
                  AND (thread_source = 'user' OR has_user_event = 1)
                """
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return fallback_codex_chats(profile.sessions_dir if profile else None)

    chats: list[tuple[float, str]] = []
    seen: set[str] = set()
    for path, recency_ms, recency, updated_ms, updated, created_ms, created in rows:
        if (
            not isinstance(path, str) or path in seen or not Path(path).is_file()
            or codex_subagent_metadata(path)
        ):
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
    return chats or fallback_codex_chats(profile.sessions_dir if profile else None)


ACTIVITY_TAIL_BYTES = 65536


def claude_session_activity(path: str) -> float:
    """Epoch seconds of the last timestamped record, falling back to the file mtime.

    Claude Code appends timestamp-less bookkeeping records (``last-prompt``, ``mode``,
    ``atis-latch``…) to old transcripts when they are merely listed or resumed, so a file's
    mtime routinely jumps weeks ahead of its real conversation activity.
    """
    mtime = os.path.getmtime(path)
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - ACTIVITY_TAIL_BYTES))
            tail = fh.read().decode("utf-8", "ignore")
    except OSError:
        return mtime
    for line in reversed(tail.splitlines()):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        stamp = parse_iso_timestamp(record.get("timestamp")) if isinstance(record, dict) else None
        if stamp:
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=dt.timezone.utc)
            return stamp.timestamp()
    return mtime


def find_all_session_entries() -> list[tuple[float, str]]:
    """Discovered sessions as (last activity epoch, path), newest first."""
    sessions: list[tuple[float, str]] = []
    profiles = discover_session_profiles()
    claude_profile = next((item for item in profiles if item.provider == "claude"), None)
    if claude_profile:
        for path in glob.glob(str(claude_profile.sessions_dir / "**" / "*.jsonl"), recursive=True):
            if "subagents" not in Path(path).parts:
                sessions.append((claude_session_activity(path), path))
    for profile in profiles:
        if profile.provider == "codex":
            sessions.extend(codex_chats(profile))
    return sorted(sessions, key=lambda item: (item[0], item[1]), reverse=True)


def find_all_sessions() -> list[str]:
    return [path for _, path in find_all_session_entries()]


def session_provider(path: str) -> str:
    if Path(path).name.startswith("rollout-") or default_codex_sessions_dir() in Path(path).parents:
        return "codex"
    return "claude"


def session_profile_id(path: str, profiles: list[SessionProfile] | None = None) -> str:
    """Identify the storage profile without changing Claude/Codex accounting."""
    path_obj = Path(path)
    candidates = profiles if profiles is not None else discover_session_profiles()
    for profile in candidates:
        if profile.sessions_dir in path_obj.parents:
            return profile.id
    return "codex" if session_provider(path) == "codex" else "claude"


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


def terminal_char_width(char: str) -> int:
    if unicodedata.category(char) in {"Mn", "Me"} or unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def terminal_width(value: str) -> int:
    """Approximate curses cell width, including combining marks used by Thai text."""
    return sum(terminal_char_width(char) for char in value)


def truncate_terminal(value: str, limit: int) -> str:
    """Truncate normalized text to a terminal-cell budget."""
    return truncate_terminal_layout(normalize_text(value), limit)


def truncate_terminal_layout(value: str, limit: int) -> str:
    """Truncate to terminal cells without collapsing intentional layout spaces."""
    if terminal_width(value) <= limit:
        return value
    budget = max(0, limit - 1)
    result: list[str] = []
    used = 0
    for char in value:
        width = terminal_char_width(char)
        if used + width > budget:
            break
        result.append(char)
        used += width
    return "".join(result) + "…"


def space_between(left: str, right: str, width: int, minimum_gap: int = 2) -> str:
    """Keep the trailing value flush-right while safely truncating the left side."""
    right_width = terminal_width(right)
    left = truncate_terminal(left, max(1, width - right_width - minimum_gap))
    gap = max(minimum_gap, width - terminal_width(left) - right_width)
    return left + " " * gap + right


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


def fmt_consumption(value: float) -> str:
    """Format the configured weighted consumption score without implying a token unit."""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:.1f}" if value % 1 else str(int(value))


from agent_monitor import parsers as _parsers

_parsers.bind(globals())

message_content = _parsers.message_content
text_blocks = _parsers.text_blocks
content_text = _parsers.content_text
has_tool_result = _parsers.has_tool_result
is_synthetic = _parsers.is_synthetic
parse_usage_observation = _parsers.parse_usage_observation
local_command_payload = _parsers.local_command_payload
is_real_user_prompt = _parsers.is_real_user_prompt
prompt_text = _parsers.prompt_text
extract_usage = _parsers.extract_usage
usage_identity = _parsers.usage_identity
explicit_sub_key = _parsers.explicit_sub_key
sub_session_key = _parsers.sub_session_key
event_label = _parsers.event_label
analyze = _parsers.analyze
FILE_TOOL_ACTIONS = _parsers.FILE_TOOL_ACTIONS
ATTACHMENT_EVIDENCE = _parsers.ATTACHMENT_EVIDENCE
AGENT_INSTRUCTION_FILES = _parsers.AGENT_INSTRUCTION_FILES
content_blocks = _parsers.content_blocks
actor_for_tool = _parsers.actor_for_tool
tool_activity_label = _parsers.tool_activity_label
tool_action_detail = _parsers.tool_action_detail
compact_action_output = _parsers.compact_action_output
attach_action_output = _parsers.attach_action_output
tool_result_text = _parsers.tool_result_text
embedded_json = _parsers.embedded_json
attach_codex_telemetry = _parsers.attach_codex_telemetry
parse_iso_timestamp = _parsers.parse_iso_timestamp
load_actor_telemetry = _parsers.load_actor_telemetry
is_agy_command = _parsers.is_agy_command
spawned_agent_engine = _parsers.spawned_agent_engine
command_option = _parsers.command_option
agy_model = _parsers.agy_model
load_agy_decision = _parsers.load_agy_decision
enrich_record = _parsers.enrich_record
enrich_analysis = _parsers.enrich_analysis
SUBAGENT_DIR_NAME = _parsers.SUBAGENT_DIR_NAME
subagent_directory = _parsers.subagent_directory
subagent_label = _parsers.subagent_label
IncrementalSessionAnalyzer = _parsers.IncrementalSessionAnalyzer
CODEX_TOOL_ACTIONS = _parsers.CODEX_TOOL_ACTIONS
codex_tool_activity = _parsers.codex_tool_activity
codex_exec_command = _parsers.codex_exec_command
codex_tool_detail = _parsers.codex_tool_detail
codex_message_text = _parsers.codex_message_text
codex_function_output_text = _parsers.codex_function_output_text
CodexSessionAnalyzer = _parsers.CodexSessionAnalyzer
create_analyzer = _parsers.create_analyzer
CACHE_TYPES = _parsers.CACHE_TYPES
cache_encode = _parsers.cache_encode
cache_decode = _parsers.cache_decode
ProfilerCache = _parsers.ProfilerCache


def session_cwd(path: str) -> str | None:
    """First working directory recorded in a session file, if any."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for _, line in zip(range(50), fh):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                payload = rec.get("payload")
                candidates = [rec.get("cwd")]
                if isinstance(payload, dict):
                    candidates.append(payload.get("cwd"))
                for candidate in candidates:
                    if isinstance(candidate, str) and candidate:
                        return candidate
    except OSError:
        pass
    return None


# Claude Code hands each session a scratchpad under /private/tmp/claude-<uid>/<encoded project
# dir>/<session id>/scratchpad. Sessions launched from there (e.g. a Codex run spawned by Claude)
# belong to the owning session's project, not to a project called "scratchpad".
SCRATCHPAD_CWD = re.compile(
    r"^/private/tmp/claude-\d+/(?P<project_dir>[^/]+)/(?P<session_id>[0-9a-fA-F-]{36})/scratchpad(?:/|$)"
)


def resolve_scratchpad_cwd(cwd: str) -> str:
    """Map a scratchpad working directory back to the project of the session that owns it."""
    match = SCRATCHPAD_CWD.match(cwd)
    if not match:
        return cwd
    owner = default_projects_dir() / match.group("project_dir") / f"{match.group('session_id')}.jsonl"
    if owner.is_file():
        resolved = session_cwd(str(owner))
        if resolved and not SCRATCHPAD_CWD.match(resolved):
            return resolved
    return cwd


def session_project_root(path: str) -> str:
    """Full working directory used to group a session into a project."""
    cwd = session_cwd(path)
    if cwd:
        return resolve_scratchpad_cwd(cwd)
    return str(Path(path).parent)


def project_display_names(roots: list[str]) -> dict[str, str]:
    """Basename per root; roots that share a basename get enough parent segments to tell apart."""
    labels = {root: Path(root).name or root for root in roots}
    depth = 1
    while True:
        counts = Counter(labels.values())
        clashing = [root for root, label in labels.items() if counts[label] > 1]
        if not clashing:
            return labels
        depth += 1
        widened = False
        for root in clashing:
            segments = [part for part in Path(root).parts if part != Path(root).anchor]
            if depth <= len(segments):
                labels[root] = "/".join(segments[-depth:])
                widened = True
        if not widened:
            return labels


def session_identifier(path: str) -> str:
    """Return the logical provider session/thread ID."""
    if session_provider(path) == "codex":
        metadata = codex_session_metadata(path)
        if metadata and metadata[0]:
            return metadata[0]
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for _, line in zip(range(50), fh):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                value = record.get("sessionId") or record.get("session_id")
                if isinstance(value, str) and value:
                    return value
    except OSError:
        pass
    return Path(path).stem


def resolve_session_path(reference: str, paths: list[str] | None = None) -> str | None:
    """Resolve a logical session ID, rollout filename, or unique prefix to a session file."""
    needle = reference.strip().removeprefix("#")
    if not needle:
        return None
    exact: list[str] = []
    prefix: list[str] = []
    for path in (paths if paths is not None else find_all_sessions()):
        logical_id = session_identifier(path)
        rollout = Path(path).stem
        identities = (logical_id, rollout, display_rollout_id(rollout))
        if needle in identities:
            exact.append(path)
        elif any(value.startswith(needle) for value in identities):
            prefix.append(path)
    return (exact or prefix or [None])[0]


def display_rollout_id(value: str) -> str:
    """Shorten UUIDs inside a rollout name for display; never use this as an identity."""
    return re.sub(
        r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4})"
        r"-[0-9a-fA-F]{12}(?=_|$)",
        r"\1",
        value,
    )


def codex_rollout_shards(path: str) -> list[str]:
    """Find rollout shards that belong to the same logical Codex thread."""
    metadata = codex_session_metadata(path)
    if not metadata or not metadata[0]:
        return [path]
    thread_id = metadata[0]
    candidates = {
        str(candidate)
        for candidate in Path(path).parent.glob(f"*{thread_id}*.jsonl")
        if codex_session_metadata(str(candidate)) == metadata
    }
    candidates.add(path)
    return sorted(candidates, key=lambda item: (os.path.getmtime(item), item))


def session_context_summary(analyses: list[Analysis]) -> tuple[int, int, float]:
    """Return latest/peak context and cache percentage across the whole session."""
    requests = [
        request.usage
        for analysis in analyses
        for prompt in analysis.prompts
        for request in prompt.requests
        if request.usage.context_total
    ]
    if not requests:
        return 0, 0, 0.0
    latest = requests[-1]
    aggregate = Usage()
    for request in requests:
        aggregate.merge(request)
    return latest.context_total, max(item.context_total for item in requests), aggregate.cache_hit_rate


def build_overall_report(
    cache: "ProfilerCache | None", window_days: int = OVERALL_WINDOW_DAYS,
    progress: Callable[[int, int], None] | None = None,
    now: dt.datetime | None = None, tz: dt.tzinfo | None = None,
) -> OverallReport:
    """Aggregate measured usage across sessions active in the last ``window_days``, by project.

    Reuses the same per-session Usage totals the Live/History views already trust
    (via the shared profiler cache when available), so the Overall page never derives
    its own, potentially divergent, consumption accounting.
    """
    entries = find_all_session_entries()
    discovered_count = len(entries)

    total = Usage()
    projects: dict[str, ProjectUsage] = {}
    provider_usage: dict[str, Usage] = {"claude": Usage(), "codex": Usage()}
    provider_sessions: Counter[str] = Counter()
    days: dict[str, DayUsage] = {}
    all_sessions: list[SessionUsage] = []
    prompt_count = 0
    session_count = 0
    unreadable = 0

    report_now = now or dt.datetime.now(dt.timezone.utc)
    if report_now.tzinfo is None:
        report_now = report_now.replace(tzinfo=dt.timezone.utc)
    # Provider timestamps are UTC; days and displayed dates follow the viewer's local clock.
    local_tz = tz or dt.datetime.now().astimezone().tzinfo
    recent_cutoff = report_now - dt.timedelta(days=30)
    window_cutoff = report_now - dt.timedelta(days=window_days)
    scoped = [
        (ordering_time, path) for ordering_time, path in entries
        if dt.datetime.fromtimestamp(ordering_time, tz=dt.timezone.utc) >= window_cutoff
    ]
    if progress:
        progress(0, len(scoped))

    for index, (ordering_time, path) in enumerate(scoped, 1):
        provider = session_provider(path)
        shard_paths = codex_rollout_shards(path) if provider == "codex" else [path]
        analyses: list[Analysis] = []
        try:
            for shard_path in shard_paths:
                analyses.append(
                    (cache.analyzer(shard_path) if cache else create_analyzer(shard_path)).analysis
                )
        except (OSError, sqlite3.Error):
            unreadable += 1
            if progress:
                progress(index, len(scoped))
            continue

        usage = Usage()
        for analysis in analyses:
            usage.merge(analysis.total_usage)
        all_prompts = [prompt for analysis in analyses for prompt in analysis.prompts]
        session_count += 1
        prompt_count += len(all_prompts)
        total.merge(usage)
        provider_usage.setdefault(provider, Usage()).merge(usage)
        provider_sessions[provider] += 1

        project_root = session_project_root(path)
        project = projects.setdefault(project_root, ProjectUsage(Path(project_root).name or project_root, root=project_root))
        project.usage.merge(usage)
        project.sessions += 1
        project.prompts += len(all_prompts)

        parsed_timestamps = [parse_iso_timestamp(prompt.timestamp) for prompt in all_prompts]
        parsed_timestamps = [
            value.replace(tzinfo=dt.timezone.utc) if value and value.tzinfo is None else value
            for value in parsed_timestamps if value
        ]
        session_time = max(parsed_timestamps) if parsed_timestamps else dt.datetime.fromtimestamp(
            ordering_time, tz=dt.timezone.utc,
        )
        label = all_prompts[0].prompt if all_prompts else Path(path).stem
        latest_context, peak_context, cache_hit_rate = session_context_summary(analyses)
        session_usage = SessionUsage(
            session_identifier(path), Path(path).stem if provider == "codex" else "",
            len(shard_paths), label, session_time.astimezone(local_tz).isoformat(), provider, usage,
            len(all_prompts), latest_context, peak_context, cache_hit_rate, path, project_root,
        )
        all_sessions.append(session_usage)
        if usage.total and session_time >= recent_cutoff:
            project.recent_sessions.append(session_usage)

        for prompt_turn in all_prompts:
            prompt_usage = prompt_turn.total_usage
            project.files.update(item.path for item in prompt_turn.files if item.path)
            stamp = parse_iso_timestamp(prompt_turn.timestamp)
            if stamp:
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=dt.timezone.utc)
                day = stamp.astimezone(local_tz).date().isoformat()
                bucket = days.setdefault(day, DayUsage(day))
                bucket.usage.merge(prompt_usage)
                bucket.providers.setdefault(provider, Usage()).merge(prompt_usage)
        if progress:
            progress(index, len(scoped))

    for root, name in project_display_names(list(projects)).items():
        projects[root].name = name

    return OverallReport(
        total=total,
        projects=sorted(projects.values(), key=lambda item: item.usage.consumption, reverse=True),
        provider_usage=provider_usage,
        provider_sessions=provider_sessions,
        session_count=session_count,
        discovered_count=discovered_count,
        prompt_count=prompt_count,
        unreadable=unreadable,
        days=[days[day] for day in sorted(days)],
        window_days=window_days,
        sessions=sorted(all_sessions, key=lambda item: item.timestamp or "", reverse=True),
    )


from agent_monitor import views as _views

_views.bind(globals())

OVERALL_SECTIONS = _views.OVERALL_SECTIONS
OVERALL_MAX_MODELS_SHOWN = _views.OVERALL_MAX_MODELS_SHOWN
OVERALL_TREND_DAYS = _views.OVERALL_TREND_DAYS
PROJECT_DETAIL_SECTIONS = _views.PROJECT_DETAIL_SECTIONS
PROJECT_DETAIL_CONTENT_WIDTH = _views.PROJECT_DETAIL_CONTENT_WIDTH
DETAIL_SECTIONS = _views.DETAIL_SECTIONS
OVERALL_CATEGORICAL_COLORS = _views.OVERALL_CATEGORICAL_COLORS
OVERALL_HTML_MAX_MODELS = _views.OVERALL_HTML_MAX_MODELS
OVERALL_HTML_MAX_TREND_DAYS = _views.OVERALL_HTML_MAX_TREND_DAYS
timestamp_hm = _views.timestamp_hm
live_feed = _views.live_feed
history_feed = _views.history_feed
pct = _views.pct
overall_lines = _views.overall_lines
overall_project_line_indices = _views.overall_project_line_indices
project_detail_lines = _views.project_detail_lines
project_session_line_indices = _views.project_session_line_indices
context_attribution = _views.context_attribution
main_actor_name = _views.main_actor_name
latest_context_size = _views.latest_context_size
latest_context_usage = _views.latest_context_usage
prompt_overview_lines = _views.prompt_overview_lines
section_line_indices = _views.section_line_indices
request_line_indices = _views.request_line_indices
sub_session_usage = _views.sub_session_usage
subagent_detail_lines = _views.subagent_detail_lines
wrapped_prefixed_lines = _views.wrapped_prefixed_lines
output_box_lines = _views.output_box_lines
request_commands = _views.request_commands
copy_to_clipboard = _views.copy_to_clipboard
default_overall_report_path = _views.default_overall_report_path
render_overall_html = _views.render_overall_html
write_overall_report = _views.write_overall_report
open_in_browser = _views.open_in_browser
open_overall_report = _views.open_overall_report


DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_HTML_PATH = Path(__file__).resolve().with_name("dashboard.html")


def dashboard_refresh_seconds() -> float:
    return max(5.0, configured_weight("AGENT_MONITOR_DASHBOARD_REFRESH", 60.0))


def usage_payload(usage: Usage) -> dict[str, Any]:
    """JSON shape for a Usage: raw components plus the weighted score, never a price."""
    return {
        "input": usage.input, "output": usage.output,
        "cache_create": usage.cache_create, "cache_read": usage.cache_read,
        "total": usage.total, "fresh": usage.fresh, "context_total": usage.context_total,
        "requests": usage.requests, "cache_hit_rate": round(usage.cache_hit_rate, 2),
        "consumption": round(usage.consumption, 2),
        "models": {
            model: {
                "requests": usage.models.get(model, 0),
                "total": usage.model_totals.get(model, 0),
                "fresh": usage.model_fresh.get(model, 0),
                "cache": usage.model_cache.get(model, 0),
                "consumption": round(usage.model_consumption.get(model, 0.0), 2),
            }
            for model in sorted(usage.model_totals.keys() | usage.models.keys())
        },
    }


def session_usage_payload(item: SessionUsage) -> dict[str, Any]:
    return {
        "id": item.session_id, "rollout_id": item.rollout_id,
        "display_id": display_rollout_id(item.rollout_id) if item.rollout_id else item.session_id,
        "shards": item.rollout_count, "label": item.label, "timestamp": item.timestamp,
        "provider": item.provider, "usage": usage_payload(item.usage), "prompts": item.prompt_count,
        "latest_context": item.latest_context, "peak_context": item.peak_context,
        "cache_hit_rate": round(item.cache_hit_rate, 2), "project_root": item.project_root,
    }


def report_payload(report: OverallReport, generated_at: str, version: int) -> dict[str, Any]:
    """Everything the detached dashboard renders, derived from the same OverallReport the TUI shows."""
    names = {project.root: project.name for project in report.projects}
    return {
        "version": version, "generated_at": generated_at,
        "weights": {
            "fresh": CONSUMPTION_CONFIG.fresh_weight, "cache": CONSUMPTION_CONFIG.cache_weight,
        },
        "window_days": report.window_days,
        "scope": {
            "sessions": report.session_count, "discovered": report.discovered_count,
            "prompts": report.prompt_count, "unreadable": report.unreadable,
            "excluded_old": report.excluded_old,
        },
        "total": usage_payload(report.total),
        "providers": {
            provider: {"usage": usage_payload(usage), "sessions": report.provider_sessions.get(provider, 0)}
            for provider, usage in report.provider_usage.items()
        },
        "projects": [
            {
                "root": project.root, "name": project.name, "usage": usage_payload(project.usage),
                "sessions": project.sessions, "prompts": project.prompts,
                "files": [[path, count] for path, count in project.files.most_common(10)],
            }
            for project in report.projects
        ],
        "days": [
            {
                "day": day.day, "usage": usage_payload(day.usage),
                "providers": {provider: usage_payload(usage) for provider, usage in day.providers.items()},
            }
            for day in report.days
        ],
        "sessions": [
            {**session_usage_payload(item), "project": names.get(item.project_root, Path(item.project_root).name)}
            for item in report.sessions
        ],
    }


def prompt_status(prompt: PromptTurn, is_last: bool, session_is_live: bool) -> str:
    if "Interrupted by user" in prompt.events:
        return "interrupted"
    if any(item.label == "Prompt completed" for item in prompt.timeline):
        return "completed"
    if is_last:
        return "running" if session_is_live else "last observed"
    return "completed"


def prompt_summary_payload(prompt: PromptTurn, position: int, shard: str, status: str) -> dict[str, Any]:
    usage = prompt.total_usage
    return {
        "position": position, "index": prompt.index, "shard": shard, "prompt": prompt.prompt,
        "timestamp": prompt.timestamp, "status": status,
        "latest_context": latest_context_size(prompt),
        "latest_cache_hit_rate": round(latest_context_usage(prompt).cache_hit_rate, 2),
        "usage": usage_payload(usage), "requests": len(prompt.requests),
        "actors": len(prompt.actors) + (1 if prompt.main.total else 0),
        "files": len(prompt.files), "events": list(prompt.events),
        "sub_sessions": len(prompt.sub_sessions),
    }


def actor_payload(actor: Actor) -> dict[str, Any]:
    started = parse_iso_timestamp(actor.started_at)
    finished = parse_iso_timestamp(actor.finished_at)
    return {
        "key": actor.key, "label": actor.label, "status": actor.status,
        "started_at": actor.started_at, "finished_at": actor.finished_at,
        "elapsed_seconds": (finished - started).total_seconds() if started and finished else None,
        "task_id": actor.task_id, "exit_code": actor.exit_code, "tool_use_id": actor.tool_use_id,
        "working_dir": actor.working_dir, "thread_id": actor.thread_id,
        "duration_seconds": actor.duration_seconds, "token_usage": dict(actor.token_usage),
        "rate_limits": actor.rate_limits, "engine": actor.engine, "model": actor.model,
        "mode": actor.mode, "lane": actor.lane, "verdict": actor.verdict,
        "decision": actor.decision, "rationale": actor.rationale,
    }


def prompt_detail_payload(prompt: PromptTurn, position: int, shard: str, status: str) -> dict[str, Any]:
    """The full prompt detail the terminal shows across its Actors/Timeline/Files/Requests pages."""
    summary = prompt_summary_payload(prompt, position, shard, status)
    timestamps = [value for value in (parse_iso_timestamp(item.timestamp) for item in prompt.timeline) if value]
    summary.update({
        "duration_seconds": max(0, round((timestamps[-1] - timestamps[0]).total_seconds())) if timestamps else None,
        "main_actor": main_actor_name(prompt) if prompt.main.total else None,
        "main": usage_payload(prompt.main),
        "sub_sessions": [
            {
                "key": sub.key, "label": sub.label, "status": sub.status,
                "thread_id": sub.thread_id, "usage": usage_payload(sub_session_usage(sub)),
            }
            for sub in prompt.sub_sessions.values()
        ],
        "actors": [actor_payload(actor) for actor in prompt.actors],
        "timeline": [
            {"timestamp": item.timestamp, "label": item.label, "kind": item.kind} for item in prompt.timeline
        ],
        "files": [
            {"timestamp": item.timestamp, "action": item.action, "path": item.path} for item in prompt.files
        ],
        "requests": [
            {
                "index": index, "timestamp": request.timestamp, "model": request.model,
                "usage": usage_payload(request.usage), "stop_reason": request.stop_reason,
                "actions": list(request.actions), "action_details": list(request.action_details),
                "action_outputs": list(request.action_outputs),
            }
            for index, request in enumerate(prompt.requests, 1)
        ],
        "context": [[name, value] for name, value in context_attribution(prompt)],
        "usage_observations": [
            {
                "before_time": item.before_time, "after_time": item.after_time,
                "before_5h": item.before_5h, "after_5h": item.after_5h,
                "before_weekly": item.before_weekly, "after_weekly": item.after_weekly,
                "delta_5h": item.delta_5h, "delta_weekly": item.delta_weekly,
                "confidence": item.confidence, "confidence_reason": item.confidence_reason,
            }
            for item in prompt.usage_observations
        ],
    })
    return summary


def load_session_analyses(path: str, cache: ProfilerCache | None) -> list[tuple[str, Analysis]]:
    """(shard stem, analysis) for every rollout shard of a logical session, chronologically."""
    shard_paths = codex_rollout_shards(path) if session_provider(path) == "codex" else [path]
    analyses = []
    for shard_path in shard_paths:
        analyzer = cache.analyzer(shard_path) if cache else create_analyzer(shard_path)
        analyses.append((Path(shard_path).stem, analyzer.analysis))
    return analyses


def codex_subagent_paths(parent_path: str, thread_ids: set[str]) -> dict[str, str]:
    """Resolve child thread IDs without promoting their rollouts into the session catalog."""
    if not thread_ids:
        return {}
    root = next(
        (profile.sessions_dir for profile in discover_session_profiles()
         if profile.provider == "codex" and profile.sessions_dir in Path(parent_path).parents),
        Path(parent_path).parent,
    )
    found: dict[str, str] = {}
    state_db = root.parent / "state_5.sqlite"
    try:
        connection = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
        try:
            placeholders = ",".join("?" for _ in thread_ids)
            rows = connection.execute(
                f"SELECT id, rollout_path FROM threads WHERE id IN ({placeholders})",
                tuple(thread_ids),
            ).fetchall()
        finally:
            connection.close()
        for thread_id, rollout_path in rows:
            if thread_id in thread_ids and isinstance(rollout_path, str) and Path(rollout_path).is_file():
                found[thread_id] = rollout_path
    except (OSError, sqlite3.Error):
        pass

    for thread_id in thread_ids - found.keys():
        candidates = list(Path(parent_path).parent.glob(f"*{thread_id}*.jsonl"))
        if not candidates and root != Path(parent_path).parent:
            candidates = list(root.glob(f"**/*{thread_id}*.jsonl"))
        if candidates:
            found[thread_id] = str(max(candidates, key=lambda item: item.stat().st_mtime_ns))
    return found


def attach_codex_subagents(
    prompts: list[PromptTurn], parent_path: str, cache: ProfilerCache | None,
) -> None:
    """Hydrate parent prompt placeholders from their hidden child rollout files."""
    thread_ids = {
        sub.thread_id or sub.key
        for prompt in prompts for sub in prompt.sub_sessions.values()
        if sub.thread_id or sub.key
    }
    for thread_id, child_path in codex_subagent_paths(parent_path, thread_ids).items():
        sub = next(
            (item for prompt in prompts for item in prompt.sub_sessions.values()
             if (item.thread_id or item.key) == thread_id),
            None,
        )
        if sub is None:
            continue
        try:
            child = (cache.analyzer(child_path) if cache else create_analyzer(child_path)).analysis
        except (OSError, sqlite3.Error):
            continue
        metadata = codex_subagent_metadata(child_path) or {}
        nickname = str(metadata.get("agent_nickname") or "")
        agent_path = str(metadata.get("agent_path") or "")
        task_name = agent_path.rsplit("/", 1)[-1]
        sub.label = " · ".join(part for part in (task_name, nickname) if part) or sub.label
        sub.path = child_path
        sub.analysis = copy.deepcopy(child)


def merged_session_analysis(
    path: str, cache: ProfilerCache | None = None,
    current_analyzer: IncrementalSessionAnalyzer | CodexSessionAnalyzer | None = None,
) -> Analysis:
    """Return one TUI analysis spanning every shard of a logical Codex session.

    Codex can continue one user-facing thread in multiple rollout files. Keep the
    physical analyzer for the selected/current file incremental, but present deep
    copies of all shard prompts with session-wide indices so navigation and detail
    lookup cannot collide on each shard's local ``1..N`` numbering.
    """
    if session_provider(path) != "codex":
        analyzer = current_analyzer or (cache.analyzer(path) if cache else create_analyzer(path))
        return analyzer.analysis

    prompts: list[PromptTurn] = []
    preamble = Usage()
    malformed = 0
    record_count = 0
    for shard_path in codex_rollout_shards(path):
        if current_analyzer is not None and Path(shard_path).resolve() == Path(path).resolve():
            analysis = current_analyzer.analysis
        else:
            analyzer = cache.analyzer(shard_path) if cache else create_analyzer(shard_path)
            analysis = analyzer.analysis
        preamble.merge(analysis.preamble)
        malformed += analysis.malformed
        record_count += analysis.record_count
        for source_prompt in analysis.prompts:
            prompt = copy.deepcopy(source_prompt)
            prompt.index = len(prompts) + 1
            prompts.append(prompt)
    attach_codex_subagents(prompts, path, cache)
    return Analysis(path, prompts, preamble, malformed, record_count, provider="codex")


def session_detail_payload(path: str, cache: ProfilerCache | None, prompt_position: int | None = None) -> dict[str, Any]:
    """Session summary with one row per prompt, or (with ``prompt_position``) one prompt in full."""
    analyses = load_session_analyses(path, cache)
    last_path = analyses[-1][1].path
    session_is_live = time.time() - os.path.getmtime(last_path) < 2
    flattened = [(shard, prompt) for shard, analysis in analyses for prompt in analysis.prompts]
    prompts = []
    for position, (shard, prompt) in enumerate(flattened, 1):
        status = prompt_status(prompt, position == len(flattened), session_is_live)
        if prompt_position is not None:
            if position == prompt_position:
                return prompt_detail_payload(prompt, position, shard, status)
            continue
        prompts.append(prompt_summary_payload(prompt, position, shard, status))
    if prompt_position is not None:
        raise KeyError(prompt_position)
    total = Usage()
    preamble = Usage()
    for _, analysis in analyses:
        total.merge(analysis.total_usage)
        preamble.merge(analysis.preamble)
    latest_context, peak_context, cache_hit_rate = session_context_summary([analysis for _, analysis in analyses])
    provider = session_provider(path)
    rollout = Path(path).stem if provider == "codex" else ""
    return {
        "id": session_identifier(path), "rollout_id": rollout,
        "display_id": display_rollout_id(rollout) if rollout else session_identifier(path),
        "provider": provider, "paths": [analysis.path for _, analysis in analyses],
        "project_root": session_project_root(path), "live": session_is_live,
        "usage": usage_payload(total), "preamble": usage_payload(preamble),
        "latest_context": latest_context, "peak_context": peak_context,
        "cache_hit_rate": round(cache_hit_rate, 2),
        "malformed": sum(analysis.malformed for _, analysis in analyses),
        "prompts": prompts,
    }


from agent_monitor import dashboard as _dashboard

_dashboard.bind(globals())


class DashboardServer(_dashboard.DashboardServer):
    """Public compatibility wrapper around the dashboard server module."""
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        _dashboard.bind(globals())
        super().__init__(*args, **kwargs)


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
    if page == "subagents":
        lines = ["Sub-agents", ""]
        for index, sub in enumerate(prompt.sub_sessions.values(), 1):
            usage = sub_session_usage(sub)
            lines.append(
                f"{index} | {sub.status:<9} · {truncate(sub.label, max(12, width - 48))} · "
                f"{fmt_tokens(usage.output)} out · {usage.requests} rounds"
            )
        return lines if prompt.sub_sessions else ["Sub-agents", "", "No sub-agents in this prompt."]
    if page.startswith("subagent:"):
        try:
            subagent_index = int(page.split(":", 1)[1])
            sub = list(prompt.sub_sessions.values())[subagent_index]
        except (ValueError, IndexError):
            return ["Sub-agent not found"]
        return subagent_detail_lines(sub, width)
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


def fit_status_bar(action_bar: str, status: str, width: int) -> str:
    """Reserve the bottom bar's right edge for global progress and notices."""
    available = max(1, width - 1)
    if not status:
        return truncate_terminal_layout(action_bar, available).ljust(available)
    status = truncate_terminal(status, max(1, available - min(14, available)))
    status_width = terminal_width(status)
    left_budget = max(0, available - status_width - 2)
    left = truncate_terminal_layout(action_bar.strip(), left_budget) if left_budget else ""
    gap = max(0, available - terminal_width(left) - status_width)
    return left + " " * gap + status


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


from agent_monitor import tui as _tui

_tui.bind(globals())


class TTYApp(_tui.TTYApp):
    """Public compatibility wrapper that refreshes patched core dependencies."""
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        _tui.bind(globals())
        super().__init__(*args, **kwargs)


def print_session_list(limit: int) -> None:
    for ordering_time, path in find_all_session_entries()[:limit]:
        modified = dt.datetime.fromtimestamp(ordering_time).strftime("%Y-%m-%d %H:%M")
        print(
            f"{modified}  {session_provider(path):<6}  "
            f"{os.path.getsize(path) / 1024 / 1024:7.1f} MiB  {path}"
        )


from agent_monitor import updater as _updater

semantic_version = _updater.semantic_version
safe_extract_release = _updater.safe_extract_release


def update_install_prefix() -> Path:
    return _updater.install_prefix(__file__)


def release_request(url: str, accept: str) -> bytes:
    return _updater.release_request(url, accept, VERSION)


def latest_release_assets() -> tuple[str, dict[str, str]]:
    return _updater.latest_release_assets(RELEASE_REPOSITORY, VERSION, release_request)


def update_agent_monitor(force: bool = False, prefix: Path | None = None) -> None:
    latest = _updater.update(
        VERSION, latest_release_assets, release_request, force=force,
        prefix=prefix or update_install_prefix(), runner=subprocess.run,
    )
    if latest is None:
        print(f"agent-monitor {VERSION} is already up to date")
    else:
        print(f"updated agent-monitor {VERSION} -> {latest}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live TTY execution profiler for Claude Code and Codex JSONL sessions.")
    parser.add_argument("-v", "--version", action="version", version=f"agent-monitor {VERSION}")
    parser.add_argument("target", nargs="?", help="Session JSONL, project directory, or 'update'")
    parser.add_argument("--list", action="store_true", help="List recent sessions")
    parser.add_argument("--list-limit", type=int, default=20)
    parser.add_argument("--force-update", action="store_true", help="Reinstall the latest release")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.target == "update":
        try:
            update_agent_monitor(force=args.force_update)
        except (OSError, ValueError, KeyError, json.JSONDecodeError, urllib.error.URLError,
                subprocess.CalledProcessError) as exc:
            raise SystemExit(f"update failed: {exc}") from exc
        return

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
