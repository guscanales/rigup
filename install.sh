#!/usr/bin/env bash
# GPU Switchboard installer for Ubuntu + one NVIDIA GPU. Safe to re-run.
#
#   ./install.sh                       install with defaults
#   SB_PORT=9000 ./install.sh          dashboard on another port
#   SB_DATA=/mnt/big ./install.sh      keep models on another disk
#
# Installs: NVIDIA driver (if missing), Docker, NVIDIA Container Toolkit, Open WebUI,
# and a systemd service running the dashboard. Models are NOT downloaded here;
# the dashboard fetches each one the first time you use it.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")" && pwd)
SB_PORT=${SB_PORT:-8090}
SB_DATA=${SB_DATA:-$ROOT/data}
WEBUI_IMAGE=$(python3 -c "import json;print(json.load(open('$ROOT/recipes.json'))['images']['openwebui'])")
say() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }

[ "$(id -u)" -ne 0 ] || { echo "Run as your normal user (it uses sudo when needed), not as root."; exit 1; }
. /etc/os-release
[ "${ID:-}" = ubuntu ] || echo "Warning: tested on Ubuntu only; you are on ${PRETTY_NAME:-unknown}. Continuing."

# 1. NVIDIA driver. A fresh driver needs a reboot, so stop here and ask to re-run.
if ! nvidia-smi >/dev/null 2>&1; then
  say "Installing the NVIDIA driver"
  sudo apt-get update -q
  sudo apt-get install -yq ubuntu-drivers-common
  sudo ubuntu-drivers install
  echo; echo "Driver installed. Reboot (sudo reboot), then run ./install.sh again."
  exit 0
fi
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader

# 2. Docker
if ! command -v docker >/dev/null; then
  say "Installing Docker"
  sudo apt-get update -q
  sudo apt-get install -yq docker.io
  sudo systemctl enable --now docker
fi
id -nG "$USER" | grep -qw docker || { sudo usermod -aG docker "$USER"; echo "Added $USER to the docker group."; }

# 3. NVIDIA Container Toolkit, so containers can see the GPU
if ! command -v nvidia-ctk >/dev/null; then
  say "Installing the NVIDIA Container Toolkit"
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
  sudo apt-get update -q
  sudo apt-get install -yq nvidia-container-toolkit
fi
sudo nvidia-ctk runtime configure --runtime=docker >/dev/null
sudo systemctl restart docker
say "Checking that Docker can use the GPU"
sudo docker run --rm --gpus all ubuntu:24.04 nvidia-smi -L

# 4. Dashboard password (stored hashed in $SB_DATA/config.json)
mkdir -p "$SB_DATA"
if ! grep -q password_hash "$SB_DATA/config.json" 2>/dev/null; then
  say "Choose a password for the dashboard"
  SB_DATA="$SB_DATA" python3 "$ROOT/server.py" set-password
fi

# 5. Open WebUI: always on, no GPU, talks to whichever LLM is loaded on :8000
if ! sudo docker ps -a --format '{{.Names}}' | grep -qx sb-webui; then
  say "Starting Open WebUI (first run downloads ~7 GB)"
  sudo docker run -d --name sb-webui --restart unless-stopped -p 3000:8080 \
    --add-host host.docker.internal:host-gateway \
    -v "$SB_DATA/open-webui:/app/backend/data" \
    -e OPENAI_API_BASE_URL=http://host.docker.internal:8000/v1 -e OPENAI_API_KEY=none -e ENABLE_OLLAMA_API=false \
    "$WEBUI_IMAGE" >/dev/null
fi

# 6. The dashboard as a service that starts at boot
say "Installing the gpu-switchboard service on port $SB_PORT"
sudo tee /etc/systemd/system/gpu-switchboard.service >/dev/null <<EOF
[Unit]
Description=GPU Switchboard dashboard
After=network-online.target docker.service
Wants=docker.service

[Service]
User=$USER
SupplementaryGroups=docker
Environment=SB_PORT=$SB_PORT
Environment=SB_DATA=$SB_DATA
ExecStart=/usr/bin/python3 $ROOT/server.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now gpu-switchboard
sudo systemctl restart gpu-switchboard

IP=$(hostname -I | awk '{print $1}')
cat <<EOF

Done. Open the dashboard from any machine on your network:

  Dashboard   http://$IP:$SB_PORT     (any username, the password you just set)
  Open WebUI  http://$IP:3000         (create the first account; it becomes admin)
  LLM API     http://$IP:8000/v1      (up while a Lab model is running)
  ComfyUI     http://$IP:8188         (up while ComfyUI mode is running)

Pick a model in the dashboard. Its files download on first use.
Gated models (FLUX 3 Klein, LTX-2.5): accept the license on Hugging Face, then paste your token in the dashboard.
EOF
