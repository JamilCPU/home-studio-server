#!/usr/bin/env bash
# Install cron jobs for YoutubeBrainwash on Pi-2
# Run: bash scripts/install-cron.sh

set -euo pipefail

INSTALL_DIR="/home/jamil/YoutubeBrainwash"
VENV="$INSTALL_DIR/.venv/bin"
LOG_DIR="$INSTALL_DIR/logs"

mkdir -p "$LOG_DIR"

echo "=== Installing YoutubeBrainwash Cron Jobs ==="

# Create the cron entries
CRON_ENTRIES=$(cat << EOF
# YoutubeBrainwash — Daily curation at 4:00 AM
0 4 * * * cd $INSTALL_DIR && $VENV/python -m src curate >> $LOG_DIR/curate.log 2>&1

# YoutubeBrainwash — Auto-tune profile weekly (Sunday 3:00 AM)
0 3 * * 0 cd $INSTALL_DIR && $VENV/python -m src tune >> $LOG_DIR/tune.log 2>&1
EOF
)

# Check if already installed
if crontab -l 2>/dev/null | grep -q "YoutubeBrainwash"; then
    echo "Cron jobs already exist. Updating..."
    crontab -l 2>/dev/null | grep -v "YoutubeBrainwash" | grep -v "brainwash" > /tmp/crontab_clean || true
    echo "$CRON_ENTRIES" >> /tmp/crontab_clean
    crontab /tmp/crontab_clean
    rm /tmp/crontab_clean
else
    echo "Installing new cron jobs..."
    (crontab -l 2>/dev/null; echo ""; echo "$CRON_ENTRIES") | crontab -
fi

echo ""
echo "=== Cron Jobs Installed ==="
echo "Current crontab:"
crontab -l
