#!/bin/zsh
# launchd wrapper: sources the key env file (600 perms) and runs one cycle.
# Secrets stay in the key file, never in the plist.
set -euo pipefail

KEY_FILE="${WEATHERBOT_KEY_FILE:-$HOME/.weatherbot.env}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [[ -f "$KEY_FILE" ]]; then
  perms=$(stat -f '%Lp' "$KEY_FILE")
  if [[ "$perms" != "600" ]]; then
    echo "refusing to run: $KEY_FILE permissions are $perms, must be 600" >&2
    exit 1
  fi
  set -a
  source "$KEY_FILE"
  set +a
fi

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "$REPO_DIR"
exec uv run weatherbot cycle
