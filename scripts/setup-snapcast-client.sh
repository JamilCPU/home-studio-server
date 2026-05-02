#!/usr/bin/env bash
# Setup Snapcast client on Pi-3 (or any additional Pi)
# Run: bash scripts/setup-snapcast-client.sh <server-ip>

set -euo pipefail

SERVER_IP="${1:-192.168.0.102}"

echo "=== YoutubeBrainwash: Snapcast Client Setup ==="
echo "Server: $SERVER_IP"

# Install Snapcast client
if ! command -v snapclient &>/dev/null; then
    echo "Installing snapclient..."
    sudo apt-get update
    sudo apt-get install -y snapclient
else
    echo "snapclient already installed"
fi

# Configure Snapcast client
SNAP_CONF="/etc/default/snapclient"
echo "Configuring snapclient..."
sudo tee "$SNAP_CONF" > /dev/null << EOF
SNAPCLIENT_OPTS="--host $SERVER_IP --port 1704"
EOF

# Enable and start
echo "Enabling snapclient service..."
sudo systemctl enable snapclient
sudo systemctl restart snapclient

echo ""
echo "=== Snapcast Client Setup Complete ==="
echo "Connected to server at $SERVER_IP:1704"
echo "Audio will play through this Pi's default audio output"
echo ""
echo "Next: pair Bluetooth to Echo Dot with:"
echo "  bash scripts/pair-bluetooth.sh <ECHO_DOT_MAC>"
