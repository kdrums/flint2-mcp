#!/usr/bin/env bash
# Run once inside the LXC after it first boots.
# Installs Python deps and registers a systemd service.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="flint2-mcp"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

echo "[1/4] Installing system packages..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git

echo "[2/4] Setting up Python venv..."
python3 -m venv "${REPO_DIR}/.venv"
"${REPO_DIR}/.venv/bin/pip" install --quiet --upgrade pip
"${REPO_DIR}/.venv/bin/pip" install --quiet -r "${REPO_DIR}/requirements.txt"

echo "[3/4] Writing systemd unit..."
cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=Flint 2 Router MCP Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${REPO_DIR}
ExecStart=${REPO_DIR}/.venv/bin/python ${REPO_DIR}/server.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

echo "[4/4] Enabling and starting service..."
systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"

echo ""
echo "Done. Check status with:  systemctl status ${SERVICE_NAME}"
echo "MCP endpoint:             http://$(hostname -I | awk '{print $1}'):8080/sse"
