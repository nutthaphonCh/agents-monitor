# tools

Standalone host tooling for Claude Code, Codex, and AGY workflows.

## Included

- `agent-monitor`: terminal session profiler for Claude Code and Codex, including spawned Claude, Codex, and AGY/Gemini background processes
- `codex-telemetry`: summarize Codex JSONL execution telemetry
- `install-apple-container`: pinned, checksum-verified Apple Container host installer

All tools use the Python 3 standard library or macOS system commands. Agent credentials and session data remain on the host and are never bundled in a release.

## Install from a release tar

```sh
tar -xzf tools-0.3.2.tar.gz
cd tools-0.3.2
scripts/install.sh
```

This installs commands under `~/.local/bin` by default. Select another prefix with `--prefix`.

```sh
scripts/install.sh --prefix /opt/nutthaphon-tools
scripts/install.sh --check
```

## Install from GitHub without cloning

Download the public release tar and checksum, verify them, then install:

```sh
curl --proto '=https' --tlsv1.2 --fail --location --remote-name-all \
  https://github.com/nutthaphonCh/agents-monitor/releases/download/v0.3.2/{tools-0.3.2.tar.gz,SHA256SUMS}
shasum -a 256 -c SHA256SUMS
tar -xzf tools-0.3.2.tar.gz
tools-0.3.2/scripts/install.sh
```

The release archive contains no agent credentials or session data.

## Background processes

Run `agent-monitor`, select a prompt in a Claude or Codex session, and press `p` to open every Claude, Codex, or AGY agent spawned by that prompt. Agents are scoped to both the selected session and prompt, grouped into active and finished sections, and ordinary yielded shell commands are excluded.

Session profiles are discovered per machine. The monitor assigns the bottom-row
keys `z`, `x`, `c`, `v`, `b`, `n`, `m` to the discovered profile array in order:
Claude (when present), the default Codex profile, then isolated `.codex-*`
profiles. For example, a machine with Claude, Codex, and Codex the 2nd shows
`z Claude`, `x Codex`, `c 2nd`; another machine can have a different mapping.

The list shows local started and finished times plus elapsed duration. Press Enter or Right Arrow for process detail. Tab switches between the realtime response, logs, and metadata; `f` toggles follow mode; Left Arrow returns to the process list.

In prompt detail, open Requests, select a model request with Up/Down, and press Enter or Right Arrow to inspect its complete tool commands. Long commands wrap instead of being truncated; Left Arrow returns to the request list.

Request detail pairs each action with a bounded output preview. Press `y` to copy the request's complete Bash commands to the macOS clipboard.

## Overall consumption page

Press `o` (or `O`) from Live or History to open **Overall**, covering every discoverable Claude and Codex session active in the last 90 days. Raw volume is not presented as the ranking unit. Projects, providers, models, sessions, and the time trend all use one configurable consumption score: `fresh × 1.0 + cache-read × 0.1`. Here, fresh includes uncached input, cache creation, and output. This score is a comparison heuristic—not price, billing, or direct compute usage. Configure the weights with `AGENT_MONITOR_FRESH_WEIGHT` and `AGENT_MONITOR_CACHE_WEIGHT`. Projects are grouped by the session's full working directory (two directories that merely share a basename, such as `work/tools` and `lg/tools`, stay separate and are labelled with enough parent segments to tell apart), and a session launched from a Claude Code scratchpad directory is attributed to the project of the session that owns that scratchpad. Daily trend buckets and session dates use the viewer's local timezone, not UTC.

The first scan runs in the background; animated progress lives in the bottom bar's global right-hand status area and remains visible from other views. Select a project with `Up`/`Down` and `Enter`/`Right`, then select one of its five highest-consumption sessions and jump to it the same way. `Esc`/`Left` restores the originating session and page. Session metadata shows latest and peak context plus the cache percentage aggregated across the complete logical session—not merely its latest request. Codex continuation shards with the same root thread ID are combined before scoring. Claude Code subagent transcripts (`<session>/subagents/agent-*.jsonl`) are read alongside the parent transcript and attributed to the prompt that spawned them via `promptId`, so sessions that fan out work to agents are not undercounted. Repeated Codex `token_count` snapshots whose cumulative total has not changed are treated as replays and counted once. File rankings use observed operation counts only and do not claim consumption attribution.

Search input uses the terminal's Unicode-aware character API, so `/` accepts Thai text. Enter `#<full-session-id>` and press `Enter` to resolve that exact logical session across Claude and Codex and open its first prompt detail immediately. A unique ID prefix, full rollout filename, or shortened rollout display ID is accepted as well. `Esc`/`Left` from a search jump restores the page and session where the search began.

On the Overall page only, `p` (or `P`) exports the same report as a single, self-contained offline HTML file — inline CSS, no external assets or network requests — to `~/Library/Caches/execution-profiler/overall-report.html`, opens it in the default browser, and shows the saved path in the bottom-right status area for 10 seconds. The project ranking itself is expandable in HTML, so model/session/file details live in one list rather than a duplicated second section. Everywhere else, `p` keeps its normal meaning: background processes for the selected prompt/session.

The scoring policy and its limitations are documented in [docs/consumption-score.md](docs/consumption-score.md). The observed Codex thread/rollout/shard model and aggregation rules are documented separately in [docs/codex-session-storage.md](docs/codex-session-storage.md).

## Detach mode

`d` on any page detaches the view on screen into the browser: it starts a dashboard server bound to `127.0.0.1` on a random port (once per run), opens the default browser at the same place you were looking at, and keeps the terminal running. From a session it opens that session; from a prompt or request detail it opens that prompt or request (Codex continuation shards resolve to the right prompt); from Overall or a project it opens the overview or the project ranking. `D` stops the server; quitting `agent-monitor` stops it as well. Set `AGENT_MONITOR_NO_BROWSER=1` to skip opening the browser and just print the URL in the status area.

The page is `dashboard.html`, one self-contained file with inline CSS and JavaScript that talks only to that loopback server. It has four tabs plus session browsing:

- **Overview**: consumption or raw-token tiles (sessions, requests, active days, current streak, peak day, top model, cache hit, output), a per-day activity heatmap, and by-provider and by-model rankings; a model row expands into its fresh / cache-read / request split.
- **Trends**: stacked daily bars by provider or by model with a legend of totals and shares.
- **Sessions**: every analyzed session with search, provider filter, and sorting by recency, consumption, peak context, prompts, or project.
- **Projects**: the same full-working-directory ranking as the terminal, expandable into model mix, top sessions, and top files.
- **Session → prompt → request**: the same drill-down as the terminal. A session lists its prompts with status, latest context and cache percentage, output, thinking rounds, actors, and file operations; a prompt shows Requests, Actors, Timeline, Files, Context attribution, Sub-sessions, and Usage observation; a request shows each action's complete command and bounded output with a copy button for its Bash commands.

The range switch (Today / 7d / 30d / 90d) and the unit switch (consumption score / raw tokens) apply everywhere; the choice is remembered per browser. Keyboard: `↑`/`↓` or `j`/`k` select, `↵`/`→` open, `←`/`esc` back, `1`-`4` switch tabs, `/` searches sessions, `u` toggles the unit, `r` reloads. The server rebuilds the report in the background every 60 seconds (`AGENT_MONITOR_DASHBOARD_REFRESH`, minimum 5) using the same accounting the terminal shows, so the dashboard never grows a second set of numbers; the page polls `/api/status` and refreshes itself when a newer report exists, and a live session re-reads on each poll. The JSON behind it is available at `/api/report`, `/api/session/<id>`, and `/api/session/<id>/prompt/<n>` on that server. The `p` export stays a static, script-free file.


## Development

```sh
python3 -m unittest tests.test_monitoring
scripts/build-release.sh 0.3.2
```
