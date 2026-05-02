#!/usr/bin/env bash
# Setup systemd service for the YoutubeBrainwash API server
# Run: bash scripts/setup-systemd-api.sh

set -euo pipefail

INSTALL_DIR="/home/jamil/YoutubeBrainwash"

echo "=== Setting up YoutubeBrainwash API Service ==="

sudo tee /etc/systemd/system/brainwash-api.service > /dev/null << EOF
[Unit]
Description=YoutubeBrainwash REST API
After=network.target snapserver.service

[Service]
Type=simple
User=jamil
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/python -m src serve
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable brainwash-api
sudo systemctl start brainwash-api

echo ""
echo "=== API Service Installed ==="
echo "Status: sudo systemctl status brainwash-api"
echo "Logs:   journalctl -u brainwash-api -f"
echo "API:    http://$(hostname -I | awk '{print $1}'):5000/api/health"
