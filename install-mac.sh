#!/usr/bin/env bash
# RigUp installer for Apple Silicon Macs. Safe to re-run.
#
#   ./install-mac.sh                       install with defaults
#   RIGUP_PORT=9000 ./install-mac.sh       dashboard on another port
#   RIGUP_DATA=/Volumes/big ./install-mac.sh   keep models on another disk
#
# Installs llama.cpp and uv (Homebrew) and a login service running the dashboard. Models are NOT
# downloaded here; the dashboard fetches each one the first time you use it. ComfyUI installs itself
# (with uv) the first time you switch to it.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")" && pwd)
RIGUP_PORT=${RIGUP_PORT:-8090}
RIGUP_DATA=${RIGUP_DATA:-$ROOT/data}
PLIST=$HOME/Library/LaunchAgents/com.rigup.dashboard.plist
say() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }

[ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ] || { echo "RigUp for Mac needs an Apple Silicon Mac (M1 or newer)."; exit 1; }
command -v brew >/dev/null || { echo "Install Homebrew first (https://brew.sh), then run this again."; exit 1; }
PY=/usr/bin/python3   # macOS's own Python is enough: the dashboard uses the standard library only

# 1. The engine: llama.cpp with Metal, from Homebrew
say "Installing llama.cpp and uv"
brew install llama.cpp uv
mkdir -p "$RIGUP_DATA"

# 2. Dashboard password (stored hashed in $RIGUP_DATA/config.json)
if ! grep -q password_hash "$RIGUP_DATA/config.json" 2>/dev/null; then
  say "Choose a password for the dashboard"
  RIGUP_DATA="$RIGUP_DATA" "$PY" "$ROOT/server.py" set-password
fi

# 3. The dashboard as a login service (launchd), restarted if it stops
say "Installing the RigUp service on port $RIGUP_PORT"
mkdir -p "$(dirname "$PLIST")"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.rigup.dashboard</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$ROOT/server.py</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>RIGUP_PORT</key><string>$RIGUP_PORT</string>
    <key>RIGUP_DATA</key><string>$RIGUP_DATA</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$RIGUP_DATA/server.log</string>
  <key>StandardErrorPath</key><string>$RIGUP_DATA/server.log</string>
</dict>
</plist>
EOF
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo localhost)
cat <<EOF

Done. Open the dashboard on this Mac or from any machine on your network:

  Dashboard   http://$IP:$RIGUP_PORT     (any username, the password you just set)
  LLM API     http://$IP:8000/v1      (up while a Lab model is running)
  ComfyUI     http://$IP:8188         (up in ComfyUI mode)

Pick a model in the dashboard. Its files download on first use.
If macOS asks whether Python may accept incoming connections, allow it so other devices can connect.
The server stops while the Mac sleeps: for an always-on box, turn on
"Prevent automatic sleeping when the display is off" in System Settings > Energy.
EOF
