#!/usr/bin/env bash
set -euo pipefail

VERSION="0.3.0"
REPOSITORY="nutthaphonCh/tools"
PREFIX="${HOME}/.local"

usage() {
  cat >&2 <<'EOF'
Usage: bootstrap-private-release.sh [--version X.Y.Z] [--prefix PATH]

Requires GITHUB_TOKEN with read-only Contents access to nutthaphonCh/tools.
EOF
}

while (($#)); do
  case "$1" in
    --version) [[ $# -ge 2 ]] || { usage; exit 2; }; VERSION="$2"; shift 2 ;;
    --prefix) [[ $# -ge 2 ]] || { usage; exit 2; }; PREFIX="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "invalid version: $VERSION" >&2; exit 2; }
[[ -n "${GITHUB_TOKEN:-}" ]] || { echo "GITHUB_TOKEN is required" >&2; exit 2; }

temp_dir="$(mktemp -d "${TMPDIR:-/tmp}/nutthaphon-tools.XXXXXX")"
cleanup() { rm -rf "$temp_dir"; }
trap cleanup EXIT HUP INT TERM

netrc="$temp_dir/github.netrc"
umask 077
printf 'machine api.github.com\nlogin token\npassword %s\n' "$GITHUB_TOKEN" >"$netrc"

archive="tools-$VERSION.tar.gz"
api="https://api.github.com/repos/$REPOSITORY/contents/dist"
headers=(-H 'Accept: application/vnd.github.raw+json')
curl --proto '=https' --tlsv1.2 --fail --location --retry 3 --netrc-file "$netrc" "${headers[@]}" \
  --output "$temp_dir/$archive" "$api/$archive?ref=v$VERSION"
curl --proto '=https' --tlsv1.2 --fail --location --retry 3 --netrc-file "$netrc" "${headers[@]}" \
  --output "$temp_dir/SHA256SUMS" "$api/SHA256SUMS?ref=v$VERSION"

expected="$(awk -v name="$archive" '$2 == name {print tolower($1)}' "$temp_dir/SHA256SUMS")"
actual="$(shasum -a 256 "$temp_dir/$archive" | awk '{print tolower($1)}')"
[[ "$expected" =~ ^[0-9a-f]{64}$ && "$actual" == "$expected" ]] || {
  echo "SHA-256 verification failed for $archive" >&2
  exit 1
}

tar -xzf "$temp_dir/$archive" -C "$temp_dir"
"$temp_dir/tools-$VERSION/scripts/install.sh" --prefix "$PREFIX"
