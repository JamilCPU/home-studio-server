#!/usr/bin/env bash
# One-time setup of Home Studio on the Pi. Run ON the Pi, from the repo copy
# (deploy-pi3.sh syncs it to ~/home-studio first), or via deploy-pi3.sh --setup.
set -euo pipefail

APP="$HOME/home-studio"
UNIT_DIR="$HOME/.config/systemd/user"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

sudo apt-get update
sudo apt-get install -y mpv bluez pipewire pipewire-pulse wireplumber \
    libspa-0.2-bluetooth python3-venv rsync

mkdir -p "$APP/data" "$APP/secrets"
chmod 700 "$APP/secrets"
# Debian trixie: the system python3 is correct for the venv.
[ -x "$APP/.venv/bin/python" ] || python3 -m venv "$APP/.venv"

sudo loginctl enable-linger "$USER"

mkdir -p "$UNIT_DIR"
cp "$SRC_DIR"/systemd/home-studio.service "$SRC_DIR"/systemd/ytdlp-update.service \
   "$SRC_DIR"/systemd/ytdlp-update.timer "$UNIT_DIR"/
systemctl --user daemon-reload
systemctl --user enable home-studio.service ytdlp-update.timer
systemctl --user start ytdlp-update.timer
# home-studio is not started here: it needs config and (optionally) cookies first.

cat <<'MSG'

Setup done. Next manual steps (see README "Finish setup"):
  1. Put cookies.txt in ~/home-studio/secrets/
  2. Edit ~/home-studio/config.local.yaml (speaker MAC, optional API key)
  3. Install Claude Code: sudo npm i -g @anthropic-ai/claude-code, then run `claude` once and /login
  4. Pair the speaker: bash scripts/pair-bluetooth.sh --find, then <MAC> hci1
  5. systemctl --user restart home-studio
MSG
