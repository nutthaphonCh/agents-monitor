#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
PREFIX="${HOME}/.local"
MODE="install"

usage() {
  cat >&2 <<'EOF'
Usage: install.sh [--prefix PATH] [--check]
EOF
}

while (($#)); do
  case "$1" in
    --prefix) [[ $# -ge 2 ]] || { usage; exit 2; }; PREFIX="$2"; shift 2 ;;
    --check) MODE="check"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

LIB_DIR="$PREFIX/lib/nutthaphon-tools"
BIN_DIR="$PREFIX/bin"

if [[ "$MODE" == "check" ]]; then
  failed=0
  for command_path in "$BIN_DIR/agent-monitor" "$BIN_DIR/codex-telemetry" "$BIN_DIR/install-apple-container"; do
    if [[ -x "$command_path" ]]; then echo "ok command=$command_path"; else echo "missing command=$command_path"; failed=1; fi
  done
  exit "$failed"
fi

mkdir -p "$LIB_DIR/tools" "$BIN_DIR"
install -m 0755 "$ROOT/monitoring.py" "$LIB_DIR/monitoring.py"
install -m 0644 "$ROOT/dashboard.html" "$LIB_DIR/dashboard.html"
install -m 0755 "$ROOT/tools/codex_telemetry.py" "$LIB_DIR/tools/codex_telemetry.py"
install -m 0755 "$ROOT/scripts/install-apple-container.sh" "$LIB_DIR/install-apple-container.sh"
ln -sfn "$LIB_DIR/monitoring.py" "$BIN_DIR/agent-monitor"
ln -sfn "$LIB_DIR/tools/codex_telemetry.py" "$BIN_DIR/codex-telemetry"
ln -sfn "$LIB_DIR/install-apple-container.sh" "$BIN_DIR/install-apple-container"
echo "installed prefix=$PREFIX"
