#!/usr/bin/env bash
# Pair, trust and connect an Echo (any Bluetooth speaker) through the USB adapter.
#
# Usage:
#   bash scripts/pair-bluetooth.sh --find [adapter=hci1]   list pairable loudspeakers
#   bash scripts/pair-bluetooth.sh <MAC> [adapter=hci1]    pair, trust, connect
#
# Say "Alexa, pair Bluetooth" first, then run --find, then pair with the MAC it prints.
set -euo pipefail

if [ "${1:-}" = "--find" ]; then
    FIND=1
    MAC=""
    ADAPTER="${2:-hci1}"
else
    FIND=0
    MAC="${1:-}"
    ADAPTER="${2:-hci1}"
    if [ -z "$MAC" ]; then
        echo "Usage: $0 <BLUETOOTH_MAC> [adapter=hci1]" >&2
        echo "       $0 --find [adapter=hci1]" >&2
        exit 1
    fi
fi

# Audio must go out through the USB dongle. Never pair on another controller.
if ! [[ "$ADAPTER" =~ ^hci[0-9]+$ ]]; then
    echo "ERROR: invalid adapter name: $ADAPTER" >&2
    exit 1
fi
if [ "$FIND" = 0 ] && ! [[ "$MAC" =~ ^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$ ]]; then
    echo "ERROR: invalid Bluetooth MAC: $MAC" >&2
    exit 1
fi

# The controller is often DOWN after boot; power it on first.
sudo hciconfig "$ADAPTER" up || true
busctl set-property org.bluez "/org/bluez/$ADAPTER" org.bluez.Adapter1 Powered b true || true

if [ "$FIND" = 1 ]; then
    # hci1 can inquire (and reports the device class) but its name lookups return
    # nothing, so resolve names via hci0 and fall back to the requested adapter.
    echo "=== Searching for loudspeakers on $ADAPTER (about 10 s) ===" >&2
    FOUND=0
    while read -r ADDR CLASS; do
        [ -n "$ADDR" ] || continue
        NAME="$(hcitool -i hci0 name "$ADDR" 2>/dev/null || true)"
        [ -n "$NAME" ] || NAME="$(hcitool -i "$ADAPTER" name "$ADDR" 2>/dev/null || true)"
        echo "$ADDR  ${NAME:-(unknown)}"
        FOUND=1
    done < <(hcitool -i "$ADAPTER" inq 2>/dev/null \
        | awk '/class:/ { for (i = 1; i <= NF; i++) if ($i == "class:") print $2, $(i + 1) }' \
        | awk '$2 ~ /0414$/ { print $1, $2 }')
    if [ "$FOUND" = 0 ]; then
        echo "No loudspeaker found. Say \"Alexa, pair Bluetooth\" and try again." >&2
        exit 1
    fi
    exit 0
fi

MAC_UP="${MAC^^}"
DEV_PATH="/org/bluez/$ADAPTER/dev_${MAC_UP//:/_}"

# bluetoothctl drops anything sent before it has connected to bluetoothd, so every
# piped session starts with a sleep; otherwise the first line (select) is lost.
echo "=== Pairing $MAC on $ADAPTER ==="
CTRL="$(busctl get-property org.bluez "/org/bluez/$ADAPTER" org.bluez.Adapter1 Address \
    | awk -F'"' '{print $2}')"
if [ -z "$CTRL" ]; then
    echo "ERROR: could not read the controller address for $ADAPTER over D-Bus." >&2
    exit 1
fi
{
    sleep 2
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

if ! busctl get-property org.bluez "$DEV_PATH" org.bluez.Device1 Connected 2>/dev/null \
        | grep -q '^b true'; then
    echo "ERROR: $MAC is not connected on $ADAPTER after pairing." >&2
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
