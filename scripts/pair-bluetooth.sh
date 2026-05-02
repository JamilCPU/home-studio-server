#!/usr/bin/env bash
# Pair and connect to an Echo Dot (or any Bluetooth speaker)
# Run: bash scripts/pair-bluetooth.sh <MAC_ADDRESS>
# Example: bash scripts/pair-bluetooth.sh AA:BB:CC:DD:EE:FF
#
# To find your Echo Dot's MAC:
# 1. Put Echo Dot in pairing mode: "Alexa, pair Bluetooth"
# 2. Run: bluetoothctl scan on
# 3. Look for your Echo Dot in the list

set -euo pipefail

MAC="${1:-}"

if [ -z "$MAC" ]; then
    echo "Usage: $0 <BLUETOOTH_MAC_ADDRESS>"
    echo ""
    echo "To find your Echo Dot's MAC address:"
    echo "  1. Say: 'Alexa, pair Bluetooth'"
    echo "  2. Run: bluetoothctl scan on"
    echo "  3. Look for 'Echo Dot' in the output"
    exit 1
fi

echo "=== Pairing Bluetooth: $MAC ==="

# Install Bluetooth utilities if needed
if ! command -v bluetoothctl &>/dev/null; then
    echo "Installing Bluetooth utilities..."
    sudo apt-get update
    sudo apt-get install -y bluetooth bluez pulseaudio-module-bluetooth
    sudo systemctl enable bluetooth
    sudo systemctl start bluetooth
fi

# Pair and connect
echo "Pairing with $MAC..."
bluetoothctl << EOF
power on
agent on
default-agent
scan on
EOF

sleep 5

bluetoothctl << EOF
pair $MAC
trust $MAC
connect $MAC
EOF

sleep 3

echo ""
echo "=== Bluetooth Pairing Complete ==="
echo "Device $MAC should now be connected"
echo ""
echo "To auto-connect on boot, the device is already trusted."
echo "To verify: bluetoothctl info $MAC"
echo ""
echo "To set as default audio output:"
echo "  pactl set-default-sink bluez_sink.${MAC//:/_}.a2dp_sink"
