# Home Studio Server

A Raspberry Pi server that plays YouTube audio to a Bluetooth speaker (via the
USB adapter `hci1`) and builds AI-curated morning sets using the `claude` CLI.

Design: [docs/superpowers/specs/2026-11-01-listening-station-design.md](docs/superpowers/specs/2026-11-01-listening-station-design.md)

## Configuration

- `config.yaml` holds defaults. Copy `config.example.local.yaml` to
  `config.local.yaml` (git-ignored) for secrets such as the YouTube Data API key
  and the speaker MAC.
- Environment overrides: `HOME_STUDIO_YOUTUBE_API_KEY`,
  `HOME_STUDIO_BT_SPEAKER_MAC`, `HOME_STUDIO_COOKIES_PATH`.
- Cookies live in `secrets/` and the database in `data/`; both are git-ignored.

## Development

    pip install -e .[dev]
    python -m pytest -q

The API listens on port 8765 by default.

## Deploying to the Pi

From WSL on the Windows PC (the host is never committed):

    HOME_STUDIO_HOST=user@pi-host bash scripts/deploy-pi3.sh

The first run installs packages, creates `~/home-studio/{.venv,secrets,data}`,
installs the systemd user units and enables lingering. Every run syncs the code
(never `secrets/`, the database or `config.local.yaml`), runs `pip install -e .`,
imports the legacy data once, and restarts the service. A minimal
`config.local.yaml` (no secrets) is created only if none exists.

The legacy import (old ratings and `data/interest_profile.json`) has already run
on the Pi, guarded by `data/.legacy-migrated`. `data/interest_profile.json` is
personal data and no longer tracked in git; a fresh install without it simply
imports nothing.

## Finish setup (Jamil)

These steps need a person; the server runs without them but reports
`bluetooth: "unconfigured"` and `cookies: "missing"` until they are done.

1. **Pair the Echo.** Say "Alexa, pair Bluetooth", then on the Pi run
   `bash ~/home-studio/scripts/pair-bluetooth.sh --find` (read-only; it powers
   `hci1` on, inquires for loudspeaker-class devices and prints `MAC  Name`
   lines). Pair with the MAC it prints:
   `bash ~/home-studio/scripts/pair-bluetooth.sh <ECHO_MAC> hci1` (say "Alexa,
   pair Bluetooth" again if the window has closed). The script verifies the
   connection over D-Bus on `hci1`; `bluetoothctl info` without `select` asks
   the built-in controller, so it reports "not available". Put the MAC in
   `~/home-studio/config.local.yaml` under `bluetooth: speaker_mac:`. The
   service powers `hci1` on and reconnects automatically at start.
2. **YouTube cookies.** In a desktop browser logged into YouTube, export
   `cookies.txt` with a "Get cookies.txt LOCALLY" type extension and copy it to
   `~/home-studio/secrets/cookies.txt` on the Pi (`chmod 600`).
3. **Claude login.** On the Pi, run `claude` once and complete `/login`.
   (If `claude` is missing: `sudo npm i -g @anthropic-ai/claude-code`.)
4. **Import the account once.** After the cookies are in place (and Claude is
   logged in, so the first taste profile can be seeded), run
   `cd ~/home-studio && .venv/bin/python -m src import-account`. After that the
   server re-imports daily at 04:00.
5. **A YouTube Data API key is strongly recommended** (`youtube: api_key:` in
   `config.local.yaml`). Without one every search and metadata lookup goes
   through yt-dlp, paced 2 s apart, which can make the morning build miss its
   few-minute window.
6. **Tailscale** for reaching the app away from home:
   `curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`,
   then note the MagicDNS name and use it in the app.
7. `systemctl --user restart home-studio`, then
   `curl -s localhost:8765/api/status` should show `ok: true`, bluetooth
   `connected` and cookies `ok`.
