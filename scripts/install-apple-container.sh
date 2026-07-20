#!/usr/bin/env bash
set -euo pipefail

VERSION="1.1.0"
MODE="install"
START_SERVICE=true
AGREE_ROSETTA=false

CURL_BIN="${CURL_BIN:-curl}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SHASUM_BIN="${SHASUM_BIN:-shasum}"
INSTALLER_BIN="${INSTALLER_BIN:-/usr/sbin/installer}"
SOFTWAREUPDATE_BIN="${SOFTWAREUPDATE_BIN:-softwareupdate}"
CONTAINER_BIN="${CONTAINER_BIN:-container}"
UNAME_BIN="${UNAME_BIN:-uname}"
SW_VERS_BIN="${SW_VERS_BIN:-sw_vers}"
SUDO_BIN="${SUDO_BIN:-sudo}"
ID_BIN="${ID_BIN:-id}"
ROSETTA_CHECK_BIN="${ROSETTA_CHECK_BIN:-}"

usage() {
  cat >&2 <<'EOF'
Usage: install-apple-container.sh [OPTIONS]

Options:
  --version VERSION                 Install this Apple Container release (default: 1.1.0)
  --agree-to-rosetta-license        Accept the Rosetta 2 license for unattended install
  --no-start                        Do not start the Apple Container system service
  --check                           Report prerequisites and installation state only
  -h, --help                        Show this help
EOF
}

while (($#)); do
  case "$1" in
    --version)
      [[ $# -ge 2 ]] || { echo "missing value for --version" >&2; exit 2; }
      VERSION="$2"
      shift 2
      ;;
    --agree-to-rosetta-license) AGREE_ROSETTA=true; shift ;;
    --no-start) START_SERVICE=false; shift ;;
    --check) MODE="check"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo "invalid version: $VERSION" >&2
  exit 2
}

architecture="$($UNAME_BIN -m 2>/dev/null || true)"
macos_major="$($SW_VERS_BIN -productVersion 2>/dev/null | cut -d. -f1 || true)"
[[ "$architecture" == "arm64" ]] || {
  echo "unsupported architecture=${architecture:-unknown}; requires arm64" >&2
  exit 1
}
[[ "$macos_major" =~ ^[0-9]+$ ]] && ((macos_major >= 26)) || {
  echo "unsupported macos_major=${macos_major:-unknown}; requires 26 or newer" >&2
  exit 1
}

rosetta_installed() {
  if [[ -n "$ROSETTA_CHECK_BIN" ]]; then
    "$ROSETTA_CHECK_BIN"
  else
    /usr/bin/pgrep oahd >/dev/null 2>&1 \
      || [[ -e /Library/Apple/usr/libexec/oah/libRosettaRuntime ]]
  fi
}

installed_version() {
  command -v "$CONTAINER_BIN" >/dev/null 2>&1 || return 1
  "$CONTAINER_BIN" --version 2>/dev/null \
    | sed -nE 's/^container( CLI)? version ([0-9]+\.[0-9]+\.[0-9]+).*/\2/p' \
    | head -1
}

if [[ "$MODE" == "check" ]]; then
  failed=0
  echo "ok architecture=$architecture"
  echo "ok macos_major=$macos_major"
  current="$(installed_version || true)"
  if [[ -n "$current" ]]; then echo "ok container_version=$current"; else echo "missing container"; failed=1; fi
  if rosetta_installed; then echo "ok rosetta=installed"; else echo "missing rosetta"; failed=1; fi
  if [[ -n "$current" ]] && "$CONTAINER_BIN" system version >/dev/null 2>&1; then
    echo "ok container_service=reachable"
  else
    echo "missing container_service"
    failed=1
  fi
  exit "$failed"
fi

if ! rosetta_installed && [[ "$AGREE_ROSETTA" != true ]]; then
  echo "Rosetta 2 is missing. Re-run with --agree-to-rosetta-license after reviewing its license." >&2
  exit 2
fi

if ! rosetta_installed; then
  "$SOFTWAREUPDATE_BIN" --install-rosetta --agree-to-license
fi

current="$(installed_version || true)"
if [[ "$current" == "$VERSION" ]]; then
  echo "Apple Container $VERSION is already installed"
else
  temp_dir="$(mktemp -d "${TMPDIR:-/tmp}/ambient-apple-container.XXXXXX")"
  cleanup_temp() { rm -rf "$temp_dir"; }
  trap cleanup_temp EXIT HUP INT TERM

  release_json="$temp_dir/release.json"
  asset_info="$temp_dir/asset.txt"
  package="$temp_dir/container-$VERSION-installer-signed.pkg"
  api_url="https://api.github.com/repos/apple/container/releases/tags/$VERSION"
  asset_name="container-$VERSION-installer-signed.pkg"

  "$CURL_BIN" --proto '=https' --tlsv1.2 --fail --location --retry 3 \
    --output "$release_json" "$api_url"
  "$PYTHON_BIN" -c '
import json, sys
release = json.load(open(sys.argv[1], encoding="utf-8"))
matches = [a for a in release.get("assets", []) if a.get("name") == sys.argv[2]]
if len(matches) != 1:
    raise SystemExit("signed installer asset missing or ambiguous")
asset = matches[0]
digest = asset.get("digest", "")
if not digest.startswith("sha256:"):
    raise SystemExit("release asset has no SHA-256 digest")
print(asset["browser_download_url"])
print(digest.removeprefix("sha256:"))
' "$release_json" "$asset_name" >"$asset_info"

  download_url="$(sed -n '1p' "$asset_info")"
  expected_sha="$(sed -n '2p' "$asset_info" | tr '[:upper:]' '[:lower:]')"
  [[ "$download_url" == "https://github.com/apple/container/releases/download/$VERSION/$asset_name" ]] || {
    echo "refusing unexpected asset URL: $download_url" >&2
    exit 1
  }
  [[ "$expected_sha" =~ ^[0-9a-f]{64}$ ]] || {
    echo "invalid published SHA-256 digest" >&2
    exit 1
  }

  "$CURL_BIN" --proto '=https' --tlsv1.2 --fail --location --retry 3 \
    --output "$package" "$download_url"
  actual_sha="$($SHASUM_BIN -a 256 "$package" | awk '{print tolower($1)}')"
  [[ "$actual_sha" == "$expected_sha" ]] || {
    echo "SHA-256 mismatch for $asset_name" >&2
    exit 1
  }
  echo "verified sha256=$actual_sha"

  if [[ "$($ID_BIN -u)" == "0" ]]; then
    "$INSTALLER_BIN" -pkg "$package" -target /
  else
    "$SUDO_BIN" "$INSTALLER_BIN" -pkg "$package" -target /
  fi
fi

if [[ "$START_SERVICE" == true ]]; then
  "$CONTAINER_BIN" system start
  "$CONTAINER_BIN" system version
else
  echo "installed; service start skipped"
fi
