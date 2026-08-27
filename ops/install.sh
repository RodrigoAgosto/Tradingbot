#!/bin/zsh
# weatherbot installer for macOS (launchd). Run from anywhere:
#   ./ops/install.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$HOME/Library/Logs/weatherbot"
KEY_FILE="$HOME/.weatherbot.env"
AGENTS_DIR="$HOME/Library/LaunchAgents"

echo "==> repo: $REPO_DIR"

echo "==> creating log dir $LOG_DIR"
mkdir -p "$LOG_DIR" "$AGENTS_DIR"

echo "==> key file"
if [[ ! -f "$KEY_FILE" ]]; then
  cp "$REPO_DIR/.env.example" "$KEY_FILE"
  echo "    created $KEY_FILE from .env.example — edit it with your values"
fi
chmod 600 "$KEY_FILE"
echo "    permissions on $KEY_FILE set to 600"

echo "==> syncing python environment (paper mode: no order client installed)"
(cd "$REPO_DIR" && uv sync)

echo "==> initializing database"
(cd "$REPO_DIR" && uv run weatherbot init-db)

echo "==> installing launchd jobs"
chmod +x "$REPO_DIR/ops/run_cycle.sh"
for name in com.rodrigo.weatherbot com.rodrigo.weatherbot.review; do
  sed -e "s|__REPO_DIR__|$REPO_DIR|g" -e "s|__HOME__|$HOME|g" \
      "$REPO_DIR/ops/$name.plist" > "$AGENTS_DIR/$name.plist"
  launchctl bootout "gui/$(id -u)/$name" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$AGENTS_DIR/$name.plist"
  echo "    loaded $name"
done

cat <<'EOF'

==> DONE. Manual steps you must do by hand:

1. Edit the key file (secrets, alerts config):
     nano ~/.weatherbot.env
   and fill in config.yaml (heartbeat URL, telegram chat_id, email addrs).

2. Keep the Mac mini awake and self-recovering (run in Terminal):
     sudo pmset -a sleep 0 disksleep 0 displaysleep 10
     sudo pmset -a autorestart 1        # auto-restart after power failure

3. System Settings (GUI):
     - General > Login Items: nothing needed (launchd user agent handles it)
     - Users & Groups > automatically log in as this user: ON
       (required so the user launchd agent runs after a reboot)
     - Energy: "Prevent automatic sleeping" ON, "Start up automatically
       after a power failure" ON (older macOS: covered by pmset above)

4. Create a healthchecks.io check: schedule = every 20 minutes,
   grace = 30 minutes. Paste its ping URL into config.yaml under
   heartbeat.url.

5. Verify the job fires:
     launchctl list | grep weatherbot
     tail -f ~/Library/Logs/weatherbot/cycle.err.log
EOF
