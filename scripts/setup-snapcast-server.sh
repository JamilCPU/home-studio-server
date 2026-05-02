#!/usr/bin/env bash
# Setup Snapcast server on Pi-2
# Run: bash scripts/setup-snapcast-server.sh

set -euo pipefail

echo "=== YoutubeBrainwash: Snapcast Server Setup (Pi-2) ==="

# Install Snapcast server
if ! command -v snapserver &>/dev/null; then
    echo "Installing snapserver..."
    sudo apt-get update
    sudo apt-get install -y snapserver
else
    echo "snapserver already installed"
fi

# Install audio dependencies
echo "Installing audio dependencies..."
sudo apt-get install -y ffmpeg

# Install yt-dlp
if ! command -v yt-dlp &>/dev/null; then
    echo "Installing yt-dlp..."
    pip3 install --user yt-dlp
else
    echo "yt-dlp already installed"
fi

# Create Snapcast FIFO pipe
FIFO_PATH="/tmp/snapfifo"
if [ ! -p "$FIFO_PATH" ]; then
    echo "Creating FIFO at $FIFO_PATH..."
    mkfifo "$FIFO_PATH"
fi

# Configure Snapcast server
SNAP_CONF="/etc/snapserver.conf"
echo "Configuring snapserver..."
sudo tee "$SNAP_CONF" > /dev/null << 'EOF'
[http]
enabled = true
bind_to_address = 0.0.0.0
port = 1780

[tcp]
enabled = true
bind_to_address = 0.0.0.0
port = 1705

[stream]
source = pipe:///tmp/snapfifo?name=YoutubeBrainwash&sampleformat=48000:16:2
buffer = 1000

[logging]
filter = *:info
EOF

# Enable and start Snapcast server
echo "Enabling snapserver service..."
sudo systemctl enable snapserver
sudo systemctl restart snapserver

echo ""
echo "=== Snapcast Server Setup Complete ==="
echo "Server running on:"
echo "  Audio stream: port 1704"
echo "  Control:      port 1705"
echo "  HTTP:         port 1780"
echo "  FIFO:         $FIFO_PATH"
