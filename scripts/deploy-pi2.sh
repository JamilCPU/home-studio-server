#!/usr/bin/env bash
# Deploy YoutubeBrainwash to Pi-2
# Run from Warstation: bash scripts/deploy-pi2.sh

set -euo pipefail

PI2_HOST="user@192.0.2.102"
REMOTE_DIR="/home/jamil/YoutubeBrainwash"

echo "=== Deploying YoutubeBrainwash to Pi-2 ==="

# Sync project files
echo "Syncing files to Pi-2..."
rsync -avz --exclude '.git' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude 'venv' \
    --exclude '.venv' \
    --exclude 'token.json' \
    --exclude 'config.local.yaml' \
    --exclude 'data/audio_cache' \
    ./ "$PI2_HOST:$REMOTE_DIR/"

# Install dependencies on Pi-2
echo "Installing dependencies..."
ssh "$PI2_HOST" << 'EOF'
cd ~/YoutubeBrainwash
python3 -m venv .venv || true
source .venv/bin/activate
pip install -e ".[dev]"
EOF

echo ""
echo "=== Deploy Complete ==="
echo "Next steps on Pi-2:"
echo "  1. Copy token.json and config.local.yaml"
echo "  2. Run: bash scripts/setup-snapcast-server.sh"
echo "  3. Run: brainwash curate"
echo "  4. Install cron: bash scripts/install-cron.sh"
