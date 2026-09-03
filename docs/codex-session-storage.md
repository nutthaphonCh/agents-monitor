# Codex session and rollout storage

This note documents the local Codex persistence model that `agent-monitor` has
observed and the accounting rules built on top of it. It describes an internal
on-disk format, not a stable public API. Re-validate these assumptions after a
Codex upgrade if filenames, metadata, or the state database schema change.

## Mental model

```text
logical thread/session
├── initial rollout file
├── continuation/rollout shard
├── continuation/rollout shard (current)
└── subagent threads (separate logical IDs; not shards)
```

A user-facing Codex conversation is a logical thread. Its durable identity is
the UUID stored in the first `session_meta` record as `payload.id` and
`payload.session_id`.

One logical thread can have more than one JSONL rollout file:

```text
rollout-<timestamp>-<thread-id>.jsonl
rollout-<timestamp>-<thread-id>_<shard-id>.jsonl
```

The UUID after the underscore identifies a later rollout shard or
continuation. It is not a second user-facing session. Observed shards for the
same thread have the same root `payload.session_id` and non-overlapping,
chronologically adjacent event ranges.

The local state database contains one row per logical thread:

```text
~/.codex/state_5.sqlite
  threads.id           = logical thread ID
  threads.rollout_path = current/latest rollout shard
```

Consequently, `threads.rollout_path` alone is enough to open the current
conversation but not necessarily enough to calculate its lifetime usage. Older
rollout shards may contain earlier requests that are absent from the current
file.

## Rollout shards versus subagents

Rollout shards and subagents must not be treated as the same thing.

| Concept | Root ID | `thread_source` | Accounting |
| --- | --- | --- | --- |
| Initial rollout | Logical thread ID | `user` | Part of the parent session |
| Continuation shard | Same logical thread ID | `user` | Merge into the parent session |
| Subagent | Its own logical thread ID | `subagent` | Exclude from top-level session discovery |

`agent-monitor` accepts a file as a shard only when its `session_meta` tuple
matches the current file's logical thread ID and thread source. A filename
substring match by itself is insufficient because it could accidentally mix a
child agent or unrelated transcript into the parent.

## `agent-monitor` accounting rules

Overall reporting follows these rules:

1. Discover one current rollout path per visible logical Codex thread from the
   state database.
2. Read the current file's root thread ID from `session_meta`.
3. Find sibling JSONL files containing that root ID.
4. Retain only files whose `session_meta` identifies the same logical thread
   and thread source.
5. Sort the retained shards chronologically.
6. Parse every shard and merge its measured request usage.
7. Count the result as one session, regardless of shard count.

## Display ID convention

Every user-facing Codex identity is based on the current `rollout-*` filename,
without `.jsonl`. For readability, the final 12-hex group of every UUID in the
name is omitted:

```text
rollout-2026-09-03T16-30-06-01a0669a-a7a5-71f1-8431-59a6fa3c6ce0
→ rollout-2026-09-03T16-30-06-01a0669a-a7a5-71f1-8431
```

For a continuation filename, the same shortening is applied to both the root
thread UUID and the UUID after the underscore. This is a display convention
only. `agent-monitor` retains the complete logical thread ID and rollout
filename for grouping and lookup; the HTML report exposes them in the display
ID's hover text. It also shows how many rollout shard files were merged.

The TUI's `/` search also accepts `#<full-thread-id>`. On Enter it resolves the
logical ID to the current rollout path and opens that session directly. This
lookup always uses retained full identities; shortened rollout IDs are only a
display convenience (a unique shortened prefix is accepted interactively).

For Claude transcripts, the session identity remains the recorded `sessionId`;
there is no Codex-style rollout shard label.

## Context metrics

Context values are derived from measured request telemetry after all rollout
shards have been ordered and merged:

- **Latest context**: input-side context on the last measured request in the
  latest shard.
- **Peak context**: largest measured request context across every shard in the
  logical session.
- **Session cache percentage**: cache-read volume divided by total input-side
  context across every measured request in every shard. It is not the latest
  request's percentage.
- **Consumption score**: configured weighted fresh and cache-read volume across
  all shards.

These are token counts, not percentages of a model's maximum context window.
The monitor does not assume a context-window limit for a model or deployment.

## Consumption score and thirty-day ranking

Overall reporting intentionally does not use raw token volume as its comparison
unit. Its default heuristic is:

```text
consumption = fresh × 1.0 + cache-read × 0.1
fresh = uncached input + cache creation + output
```

The weights are configuration, not accounting truth:

```sh
AGENT_MONITOR_FRESH_WEIGHT=1.0
AGENT_MONITOR_CACHE_WEIGHT=0.1
```

This score must not be interpreted as price, billed usage, energy, latency, or
direct compute consumption. It exists only to make relative rankings less
misleading than treating cache reads and fresh work as equal. Overall project,
provider, model, session, and time-trend percentages are recalculated from this
score. See [consumption-score.md](consumption-score.md) for the shared
cross-provider scoring contract and configuration.

“Top 5 sessions by consumption · last 30 days” ranks logical sessions, not
individual prompts or rollout files. Eligibility uses the session's latest
observed prompt timestamp, so a long-running thread remains eligible when it was
active during the window even if its initial rollout file was created earlier.

The task summary shown for a ranked session is its first observed user prompt.
The displayed date is the latest observed activity date, not necessarily the
rollout creation date.

## File attribution limitation

Provider telemetry and file events are independent. File operations such as
Read, Edit, or Write do not carry a measured mapping to consumption.

Therefore:

- consumption can be scored for requests, sessions, projects, providers, and models;
- “Top files” is ranked by observed file-operation count;
- the report must not claim measured consumption per file.

## Diagnostic checks

Inspect the metadata at the beginning of a rollout:

```sh
head -n 1 ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl | jq .
```

Inspect the state database's current pointer for a thread:

```sh
sqlite3 -header -column ~/.codex/state_5.sqlite \
  "SELECT id, rollout_path, thread_source, created_at, updated_at
   FROM threads
   WHERE id = '<thread-id>';"
```

When debugging an apparent duplicate or undercount, compare the candidate
files' root `payload.session_id`, `thread_source`, first/last timestamps, and
request-usage events before changing the grouping rule.
