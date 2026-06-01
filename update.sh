#!/usr/bin/env bash
# Updates ED Claude Connector to the latest version, then re-runs install.sh
# (idempotent). Works whether you cloned with git or downloaded the tarball.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "Updating ED Claude Connector in $REPO"

if [[ -d "$REPO/.git" ]]; then
  echo "git checkout detected - pulling latest..."
  git -C "$REPO" pull --ff-only
else
  echo "Downloading latest from GitHub..."
  tmp="$(mktemp -d)"
  curl -sL "https://github.com/Left47/EDMC-MCP/archive/refs/heads/main.tar.gz" -o "$tmp/main.tar.gz"
  tar -xzf "$tmp/main.tar.gz" -C "$tmp"
  src="$tmp/EDMC-MCP-main"
  for sub in plugin mcp; do
    cp -R "$src/$sub/." "$REPO/$sub/"
  done
  for f in install.sh update.sh README.md; do
    [[ -f "$src/$f" ]] && cp "$src/$f" "$REPO/$f"
  done
  rm -rf "$tmp"
fi

echo "Re-running installer..."
chmod +x "$REPO/install.sh"
"$REPO/install.sh" "$@"
echo "Update complete. Restart EDMarketConnector and Claude Desktop."
