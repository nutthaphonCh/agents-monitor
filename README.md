# tools

Standalone host tooling for Claude Code, Codex, and AGY workflows.

## Included

- `agent-monitor`: terminal session profiler for Claude Code and Codex, including spawned Claude, Codex, and AGY/Gemini background processes
- `codex-telemetry`: summarize Codex JSONL execution telemetry
- `install-apple-container`: pinned, checksum-verified Apple Container host installer

All tools use the Python 3 standard library or macOS system commands. Agent credentials and session data remain on the host and are never bundled in a release.

## Install from a release tar

```sh
tar -xzf tools-0.3.0.tar.gz
cd tools-0.3.0
scripts/install.sh
```

This installs commands under `~/.local/bin` by default. Select another prefix with `--prefix`.

```sh
scripts/install.sh --prefix /opt/nutthaphon-tools
scripts/install.sh --check
```

## Private GitHub bootstrap without cloning

Create a fine-grained token with read-only Contents access to this repository. Download only the bootstrap script through the GitHub API, then let it fetch and verify the release tar:

```sh
read -s GITHUB_TOKEN
export GITHUB_TOKEN
curl --proto '=https' --tlsv1.2 --fail --location \
  --netrc-file <(printf 'machine api.github.com\nlogin token\npassword %s\n' "$GITHUB_TOKEN") \
  -H 'Accept: application/vnd.github.raw+json' \
  -o /tmp/bootstrap-tools.sh \
  'https://api.github.com/repos/nutthaphonCh/tools/contents/scripts/bootstrap-private-release.sh?ref=v0.3.0'
chmod 700 /tmp/bootstrap-tools.sh
/tmp/bootstrap-tools.sh --version 0.3.0
```

The bootstrap keeps the token out of curl's argument list, downloads the release tar and checksum through the GitHub Contents API, verifies SHA-256, extracts into a temporary directory, and runs its installer. It does not create a Git checkout.

## Background processes

Run `agent-monitor`, select a prompt in a Claude or Codex session, and press `p` to open every Claude, Codex, or AGY agent spawned by that prompt. Agents are scoped to both the selected session and prompt, grouped into active and finished sections, and ordinary yielded shell commands are excluded.

The list shows local started and finished times plus elapsed duration. Press Enter or Right Arrow for process detail. Tab switches between the realtime response, logs, and metadata; `f` toggles follow mode; Left Arrow returns to the process list.

In prompt detail, open Requests, select a model request with Up/Down, and press Enter or Right Arrow to inspect its complete tool commands. Long commands wrap instead of being truncated; Left Arrow returns to the request list.

Request detail pairs each action with a bounded output preview. Press `y` to copy the request's complete Bash commands to the macOS clipboard.

## Overall consumption page

Press `o` (or `O`) from Live or History to open **Overall**, covering every discoverable Claude and Codex session active in the last 90 days. Raw volume is not presented as the ranking unit. Projects, providers, models, sessions, and the time trend all use one configurable consumption score: `fresh × 1.0 + cache-read × 0.1`. Here, fresh includes uncached input, cache creation, and output. This score is a comparison heuristic—not price, billing, or direct compute usage. Configure the weights with `AGENT_MONITOR_FRESH_WEIGHT` and `AGENT_MONITOR_CACHE_WEIGHT`. Projects are grouped by the session's full working directory (two directories that merely share a basename, such as `work/tools` and `lg/tools`, stay separate and are labelled with enough parent segments to tell apart), and a session launched from a Claude Code scratchpad directory is attributed to the project of the session that owns that scratchpad. Daily trend buckets and session dates use the viewer's local timezone, not UTC.

The first scan runs in the background; animated progress lives in the bottom bar's global right-hand status area and remains visible from other views. Select a project with `Up`/`Down` and `Enter`/`Right`, then select one of its five highest-consumption sessions and jump to it the same way. `Esc`/`Left` restores the originating session and page. Session metadata shows latest and peak context plus the cache percentage aggregated across the complete logical session—not merely its latest request. Codex continuation shards with the same root thread ID are combined before scoring. File rankings use observed operation counts only and do not claim consumption attribution.

Search input uses the terminal's Unicode-aware character API, so `/` accepts Thai text. Enter `#<full-session-id>` and press `Enter` to resolve that exact logical session across Claude and Codex and open its first prompt detail immediately. A unique ID prefix, full rollout filename, or shortened rollout display ID is accepted as well. `Esc`/`Left` from a search jump restores the page and session where the search began.

On the Overall page only, `p` (or `P`) exports the same report as a single, self-contained offline HTML file — inline CSS, no external assets or network requests — to `~/Library/Caches/execution-profiler/overall-report.html`, opens it in the default browser, and shows the saved path in the bottom-right status area for 10 seconds. The project ranking itself is expandable in HTML, so model/session/file details live in one list rather than a duplicated second section. Everywhere else, `p` keeps its normal meaning: background processes for the selected prompt/session.

The scoring policy and its limitations are documented in [docs/consumption-score.md](docs/consumption-score.md). The observed Codex thread/rollout/shard model and aggregation rules are documented separately in [docs/codex-session-storage.md](docs/codex-session-storage.md).

## Development

```sh
python3 -m unittest tests.test_monitoring
scripts/build-release.sh 0.3.0
```
