#!/usr/bin/env bash
# Installs the Elite Dangerous -> Claude connector on Linux (e.g. running the
# game via Proton/Steam Play, with EDMarketConnector under Linux). On Windows,
# use install.bat instead.
#   1. Copies the EDClaudeConnector plugin into the EDMarketConnector plugins folder
#   2. Installs the MCP server's Python dependency (mcp)
#   3. Wires up the "elite-dangerous" MCP server in Claude Desktop's config
#      (merges into existing config; leaves other servers untouched)
#
# Usage:  ./install.sh [--state-file /path/to/state.json]
#
# Override targets for testing/power users via env vars:
#   EDMC_PLUGIN_DIR=...   CLAUDE_CONFIG=...   PYTHON=...
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --state-file) STATE_FILE="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

echo "Elite Dangerous MCP installer"
echo "Repo: $REPO"

# --- Resolve paths ----------------------------------------------------------
if [[ "$(uname -s)" == "Darwin" ]]; then
  echo "Elite Dangerous is no longer supported on macOS. Use Windows (install.bat) or Linux." >&2
  exit 1
fi
DEFAULT_PLUGIN_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/EDMarketConnector/plugins"
DEFAULT_CLAUDE_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/Claude/claude_desktop_config.json"
PLUGIN_ROOT="${EDMC_PLUGIN_DIR:-$DEFAULT_PLUGIN_DIR}"
CLAUDE_CONFIG="${CLAUDE_CONFIG:-$DEFAULT_CLAUDE_CONFIG}"
PYTHON="${PYTHON:-$(command -v python3 || command -v python || true)}"

[[ -n "$PYTHON" ]] || { echo "No python3 found on PATH. Install Python 3.10+ and re-run." >&2; exit 1; }

# --- 1. Install the plugin ---------------------------------------------------
PLUGIN_SRC="$REPO/plugin/EDClaudeConnector"
PLUGIN_DEST="$PLUGIN_ROOT/EDClaudeConnector"
[[ -d "$PLUGIN_SRC" ]] || { echo "Plugin source not found at $PLUGIN_SRC" >&2; exit 1; }
mkdir -p "$PLUGIN_DEST"
cp -R "$PLUGIN_SRC/." "$PLUGIN_DEST/"
# Record where we installed from so the plugin's "click to update" can find
# update.sh (the plugin folder otherwise has no idea where the repo lives).
printf '{"repo": "%s", "version_installed_from": "install.sh"}\n' "$REPO" > "$PLUGIN_DEST/install_info.json"
echo "[1/3] Plugin installed -> $PLUGIN_DEST"

# --- 2. Create an isolated venv and install the MCP dependency --------------
# A dedicated venv avoids PEP 668 "externally-managed-environment" errors
# (Homebrew Python, Ubuntu 24.04+, Debian 12+) and isolates the dependency.
echo "Using Python: $PYTHON"
VENV="$REPO/.venv"
"$PYTHON" -m venv "$VENV"
VENV_PY="$VENV/bin/python"
"$VENV_PY" -m pip install -q --upgrade pip
"$VENV_PY" -m pip install -q -r "$REPO/mcp/requirements.txt"
# Best-effort reference-data refresh; the repo ships generated copies as fallback.
"$VENV_PY" "$REPO/mcp/update_references.py" >/dev/null 2>&1 && echo "        reference data refreshed" \
  || echo "        (kept bundled reference data; network refresh skipped)"
echo "[2/3] MCP dependency installed into $VENV"

# --- 3. Merge Claude Desktop config (via python for safe JSON handling) ------
SERVER_PATH="$REPO/mcp/ed_claude_mcp.py"
mkdir -p "$(dirname "$CLAUDE_CONFIG")"
PY_BIN="$VENV_PY" SERVER="$SERVER_PATH" CONFIG="$CLAUDE_CONFIG" STATE="$STATE_FILE" "$VENV_PY" - <<'PYEOF'
import json, os
cfg_path = os.environ["CONFIG"]
try:
    with open(cfg_path) as fh:
        cfg = json.load(fh)
except (FileNotFoundError, json.JSONDecodeError):
    cfg = {}
cfg.setdefault("mcpServers", {})
# Pin an absolute snapshot path so the server never depends on `~` expansion.
# Priority: STATE arg > a path already pinned in the config (preserve on update)
# > default. This keeps a custom snapshot path working across re-installs.
default = os.path.join(os.path.expanduser("~"), ".elite-dangerous-claude", "state.json")
try:
    existing = cfg["mcpServers"].get("elite-dangerous", {}).get("env", {}).get("EDCLAUDE_STATE_FILE")
except AttributeError:
    existing = None
state = os.environ.get("STATE") or existing or default
if existing and not os.environ.get("STATE") and existing != default:
    print(f"        preserving custom snapshot path: {state}")
server = {
    "command": os.environ["PY_BIN"],
    "args": [os.environ["SERVER"]],
    "env": {"EDCLAUDE_STATE_FILE": state},
}
cfg["mcpServers"]["elite-dangerous"] = server  # add/replace only this key
with open(cfg_path, "w") as fh:
    json.dump(cfg, fh, indent=2)
print(f"[3/3] Claude Desktop config updated -> {cfg_path}")
PYEOF

cat <<'DONE'

Done.
Next:
  1. Restart EDMarketConnector (look for 'Elite Dangerous MCP: Running' on the main window).
  2. Restart Claude Desktop.
  3. Launch Elite Dangerous, then ask Claude about your loadout or materials.
DONE
