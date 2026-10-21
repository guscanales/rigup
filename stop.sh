#!/usr/bin/env bash
# Stop everything RigUp runs: the model or ComfyUI, Open WebUI (Linux) and the dashboard.
# Nothing is deleted. Works on Linux and macOS.
set -uo pipefail

ROOT=$(cd "$(dirname "$0")" && pwd)
RIGUP_DATA=${RIGUP_DATA:-$ROOT/data}
PLIST=$HOME/Library/LaunchAgents/com.rigup.dashboard.plist

if [ "$(uname -s)" = Darwin ]; then
  # The engine (llama-server or ComfyUI) runs in its own process group; its pid is in engine.json
  pid=$(sed -n 's/.*"pid": *\([0-9]*\).*/\1/p' "$RIGUP_DATA/engine.json" 2>/dev/null)
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    kill -TERM -- "-$pid" 2>/dev/null
    for _ in $(seq 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
    kill -KILL -- "-$pid" 2>/dev/null
    echo "Stopped the running model"
  fi
  rm -f "$RIGUP_DATA/engine.json"
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null && echo "Stopped the dashboard"
  echo
  echo "Everything is stopped. The dashboard starts again at your next login, or now with:"
  echo "  launchctl bootstrap gui/\$(id -u) $PLIST"
else
  sudo systemctl stop rigup 2>/dev/null && echo "Stopped the dashboard"
  for c in rigup-llm rigup-comfy rigup-webui; do
    docker stop -t 30 "$c" >/dev/null 2>&1 && echo "Stopped $c"   # ComfyUI needs >10s to release VRAM cleanly
  done
  docker rm rigup-llm rigup-comfy >/dev/null 2>&1   # the dashboard recreates these; Open WebUI keeps its container
  echo
  echo "Everything is stopped and stays stopped until you start it. The dashboard comes back on the next boot, or now with:"
  echo "  sudo systemctl start rigup && docker start rigup-webui"
fi
