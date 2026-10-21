#!/usr/bin/env bash
# Remove RigUp: stops everything, removes the dashboard service and containers, then asks
# before deleting the downloaded models. Works on Linux and macOS.
set -uo pipefail

ROOT=$(cd "$(dirname "$0")" && pwd)
RIGUP_DATA=${RIGUP_DATA:-$ROOT/data}
export RIGUP_DATA

"$ROOT/stop.sh" >/dev/null
echo "Stopped everything"

if [ "$(uname -s)" = Darwin ]; then
  rm -f "$HOME/Library/LaunchAgents/com.rigup.dashboard.plist" && echo "Removed the login service"
else
  sudo systemctl disable rigup 2>/dev/null
  sudo rm -f /etc/systemd/system/rigup.service && sudo systemctl daemon-reload && echo "Removed the rigup service"
  docker rm -f rigup-llm rigup-comfy rigup-webui >/dev/null 2>&1 && echo "Removed the containers"
fi

if [ -d "$RIGUP_DATA" ]; then
  echo
  printf "Delete %s (%s: every downloaded model, ComfyUI outputs%s)? [y/N] " \
    "$RIGUP_DATA" "$(du -sh "$RIGUP_DATA" 2>/dev/null | cut -f1)" "$([ "$(uname -s)" = Darwin ] || echo ', chat history')"
  read -r ans
  if [ "$ans" = y ] || [ "$ans" = Y ]; then
    if [ "$(uname -s)" = Darwin ]; then rm -rf "$RIGUP_DATA"; else sudo rm -rf "$RIGUP_DATA"; fi   # engines write some files as root
    echo "Deleted $RIGUP_DATA"
  else
    echo "Kept $RIGUP_DATA"
  fi
fi

echo
echo "RigUp is removed. This folder ($ROOT) is still here; delete it when you like."
if [ "$(uname -s)" = Darwin ]; then
  echo "llama.cpp and uv stay installed: brew uninstall llama.cpp uv"
else
  echo "Docker, the NVIDIA driver and the container toolkit stay installed. To free the engine images (tens of GB):"
  echo "  docker image rm \$(docker image ls --format '{{.Repository}}:{{.Tag}}' | grep -E 'vllm|llama.cpp|comfyui|open-webui')"
fi
