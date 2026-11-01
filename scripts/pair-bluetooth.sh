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

# Resolve the controller address of the requested adapter. Never fall back to
# the default controller: audio must go out through this adapter.
CTRL=""
METHOD=""
CTRL="$( (hciconfig "$ADAPTER" 2>/dev/null || true) | awk '/BD Address/ {print $3}' )"
[ -n "$CTRL" ] && METHOD="hciconfig"
if [ -z "$CTRL" ] && [ -r "/sys/class/bluetooth/$ADAPTER/address" ]; then
    CTRL="$(tr 'a-z' 'A-Z' < "/sys/class/bluetooth/$ADAPTER/address" || true)"
    [ -n "$CTRL" ] && METHOD="sysfs"
fi
if [ -z "$CTRL" ]; then
    CTRL="$( (btmgmt --index "${ADAPTER#hci}" info 2>/dev/null || true)         | awk '/addr/ {print $2; exit}' )"
    [ -n "$CTRL" ] && METHOD="btmgmt"
fi
if [ -z "$CTRL" ]; then
    echo "ERROR: could not find the controller address for $ADAPTER (tried hciconfig, sysfs, btmgmt)." >&2
    exit 1
fi
echo "Adapter $ADAPTER resolved via $METHOD"
if [ "${PAIR_RESOLVE_ONLY:-}" = "1" ]; then
    echo "METHOD=$METHOD ADDRESS=$CTRL"
    exit 0
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

if ! bluetoothctl info "$MAC" | grep -q 'Connected: yes'; then
    echo "ERROR: $MAC is not connected after pairing." >&2
    exit 1
fi

# Find the speaker's PipeWire sink (it can take a moment to appear).
NAME=""
for _ in 1 2 3 4 5; do
    NAME="$(pactl list short sinks | grep -i bluez | awk '{print $2; exit}' || true)"
    [ -n "$NAME" ] && break
    sleep 2
done
if [ -z "$NAME" ]; then
    echo "ERROR: no bluez sink found; the speaker may not have an audio profile yet." >&2
    exit 1
fi
pactl set-default-sink "$NAME"
echo "Default sink: $NAME"
echo "Done: $MAC connected on $ADAPTER."
