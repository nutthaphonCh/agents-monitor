# tools

Standalone host tooling for Claude Code, Codex, and AGY workflows.

## Included

- `agent-monitor`: terminal session profiler for Claude Code and Codex, including spawned Codex and AGY/Gemini actors
- `codex-telemetry`: summarize Codex JSONL execution telemetry
- `install-apple-container`: pinned, checksum-verified Apple Container host installer

All tools use the Python 3 standard library or macOS system commands. Agent credentials and session data remain on the host and are never bundled in a release.

## Install from a release tar

```sh
tar -xzf tools-0.1.0.tar.gz
cd tools-0.1.0
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
  'https://api.github.com/repos/nutthaphonCh/tools/contents/scripts/bootstrap-private-release.sh?ref=v0.1.0'
chmod 700 /tmp/bootstrap-tools.sh
/tmp/bootstrap-tools.sh --version 0.1.0
```

The bootstrap keeps the token out of curl's argument list, downloads the release tar and checksum through the GitHub Contents API, verifies SHA-256, extracts into a temporary directory, and runs its installer. It does not create a Git checkout.

## Development

```sh
python3 -m unittest tests.test_monitoring
scripts/build-release.sh 0.1.0
```
