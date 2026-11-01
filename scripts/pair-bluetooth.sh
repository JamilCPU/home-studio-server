#!/usr/bin/env bash
# Pair, trust and connect an Echo (any Bluetooth speaker) through the USB adapter.
# Usage: bash scripts/pair-bluetooth.sh <MAC> [adapter=hci1]
# Find the MAC: say "Alexa, pair Bluetooth", then run `bluetoothctl scan on`
# (use `select <controller>` first) and look for the Echo.
set -euo pipefail

MAC="${1:-}"
ADAPTER="${2:-hci1}"
if [ -z "$MAC" ]; then
    echo "Usage: $0 <BLUETOOTH_MAC> [adapter=hci1]" >&2
    exit 1
fi

# Map hciN -> controller address for `bluetoothctl select`.
CTRL="$(hciconfig "$ADAPTER" 2>/dev/null | awk '/BD Address/ {print $3}')"
if [ -z "$CTRL" ]; then
    CTRL="$(bluetoothctl list | awk -v a="$ADAPTER" 'NR==1 {print $2}')"
    echo "Could not map $ADAPTER via hciconfig; falling back to the first controller." >&2
fi

echo "=== Pairing $MAC on $ADAPTER ==="
{
    echo "select $CTRL"
    echo "power on"
    echo "agent on"
    echo "default-agent"
    echo "scan on"
    sleep 8
    echo "pair $MAC"
    sleep 6
    echo "trust $MAC"
    echo "connect $MAC"
    sleep 5
    echo "scan off"
    echo "quit"
} | bluetoothctl

# Make the speaker the default PipeWire sink (give the A2DP sink a moment to appear).
SINK="bluez_output.${MAC//:/_}.1"
for _ in 1 2 3 4 5; do
    NAME="$(pactl list short sinks | awk -v m="${MAC//:/_}" '$2 ~ m {print $2; exit}')"
    [ -n "$NAME" ] && break
    sleep 2
done
SINK="${NAME:-$SINK}"
pactl set-default-sink "$SINK" && echo "Default sink: $SINK"

echo "Verify: bluetoothctl info $MAC   (expect 'Connected: yes')"
