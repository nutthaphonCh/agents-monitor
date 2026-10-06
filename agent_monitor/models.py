from __future__ import annotations

import os

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def configured_weight(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, default))
        return value if value >= 0 else default
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class ConsumptionConfig:
    fresh_weight: float = configured_weight("AGENT_MONITOR_FRESH_WEIGHT", 1.0)
    cache_weight: float = configured_weight("AGENT_MONITOR_CACHE_WEIGHT", 0.1)


CONSUMPTION_CONFIG = ConsumptionConfig()


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_create: int = 0
    cache_read: int = 0
    requests: int = 0
    models: Counter[str] = field(default_factory=Counter)
    model_totals: Counter[str] = field(default_factory=Counter)
    model_fresh: Counter[str] = field(default_factory=Counter)
    model_cache: Counter[str] = field(default_factory=Counter)

    def add(self, model: str, inp: int, out: int, cache_create: int, cache_read: int) -> None:
        self.input += inp
        self.output += out
        self.cache_create += cache_create
        self.cache_read += cache_read
        self.requests += 1
        self.models[model] += 1
        self.model_totals[model] += inp + out + cache_create + cache_read
        self.model_fresh[model] += inp + out + cache_create
        self.model_cache[model] += cache_read

    def merge(self, other: "Usage") -> None:
        self.input += other.input
        self.output += other.output
        self.cache_create += other.cache_create
        self.cache_read += other.cache_read
        self.requests += other.requests
        self.models.update(other.models)
        self.model_totals.update(other.model_totals)
        self.model_fresh.update(other.model_fresh)
        self.model_cache.update(other.model_cache)

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
    def fresh(self) -> int:
        return self.input + self.output + self.cache_create

    @property
    def consumption(self) -> float:
        return self.fresh_consumption + self.cache_consumption

    @property
    def fresh_consumption(self) -> float:
        return self.fresh * CONSUMPTION_CONFIG.fresh_weight

    @property
    def cache_consumption(self) -> float:
        return self.cache_read * CONSUMPTION_CONFIG.cache_weight

    @property
    def model_consumption(self) -> Counter[str]:
        models = self.model_fresh.keys() | self.model_cache.keys()
        return Counter({
            model: (
                self.model_fresh[model] * CONSUMPTION_CONFIG.fresh_weight
                + self.model_cache[model] * CONSUMPTION_CONFIG.cache_weight
            )
            for model in models
        })

    @property
    def primary_model(self) -> str:
        return self.models.most_common(1)[0][0] if self.models else "unknown"


@dataclass
class SubSession:
    key: str
    label: str
    first_sequence: int
    usage: Usage = field(default_factory=Usage)
    status: str = "observed"
    thread_id: str | None = None
    path: str | None = None
    analysis: Analysis | None = field(default=None, repr=False)


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
    prompt_id: str | None = None

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


OVERALL_WINDOW_DAYS = 90
PROFILE_HOTKEYS = "zxcvbnm"


@dataclass(frozen=True)
class SessionProfile:
    id: str
    label: str
    provider: str
    sessions_dir: Path


@dataclass
class SessionUsage:
    session_id: str
    rollout_id: str
    rollout_count: int
    label: str
    timestamp: str | None
    provider: str
    usage: Usage = field(default_factory=Usage)
    prompt_count: int = 0
    latest_context: int = 0
    peak_context: int = 0
    cache_hit_rate: float = 0.0
    path: str = ""
    project_root: str = ""


@dataclass
class ProjectUsage:
    name: str
    usage: Usage = field(default_factory=Usage)
    sessions: int = 0
    prompts: int = 0
    recent_sessions: list[SessionUsage] = field(default_factory=list)
    files: Counter[str] = field(default_factory=Counter)
    root: str = ""


@dataclass
class DayUsage:
    day: str
    usage: Usage = field(default_factory=Usage)
    providers: dict[str, Usage] = field(default_factory=dict)


@dataclass
class OverallReport:
    total: Usage
    projects: list[ProjectUsage]
    provider_usage: dict[str, Usage]
    provider_sessions: Counter[str]
    session_count: int
    discovered_count: int
    prompt_count: int
    unreadable: int
    days: list[DayUsage]
    window_days: int = OVERALL_WINDOW_DAYS
    sessions: list[SessionUsage] = field(default_factory=list)

    @property
    def excluded_old(self) -> int:
        return max(0, self.discovered_count - self.session_count - self.unreadable)


MODEL_TYPES = (
    Usage, SubSession, Actor, TimelineItem, FileActivity, RequestInfo,
    UsageObservation, PromptTurn, Analysis,
)
