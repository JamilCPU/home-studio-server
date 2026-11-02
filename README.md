# Home Studio Server

A Raspberry Pi server that plays YouTube audio to a Bluetooth speaker (via the
USB adapter `hci1`) and builds AI-curated morning sets using the `claude` CLI.

Design: [docs/superpowers/specs/2026-11-01-listening-station-design.md](docs/superpowers/specs/2026-11-01-listening-station-design.md)

## Configuration

- `config.yaml` holds defaults. Copy `config.example.local.yaml` to
  `config.local.yaml` (git-ignored) for secrets such as the YouTube Data API key
  and the speaker MAC.
- YouTube account sign-in: `youtube.oauth_client_id` and `youtube.oauth_client_secret`
  in `config.local.yaml`; the token is written to `youtube.oauth_token_path`
  (default `secrets/youtube_oauth.json`).
- Environment overrides: `HOME_STUDIO_YOUTUBE_API_KEY`,
  `HOME_STUDIO_BT_SPEAKER_MAC`, `HOME_STUDIO_COOKIES_PATH`.
- Cookies live in `secrets/` and the database in `data/`; both are git-ignored.

## Development

    pip install -e .[dev]
    python -m pytest -q

The API listens on port 8765 by default.

## MCP server

`mcp/` holds `homestudio-mcp`, a read-only MCP server over this API (taste, ratings,
listening history, now playing, queue) for the assistant gateway. It runs beside the gateway,
not on the Pi; CI publishes its image to `ghcr.io/jamilcpu/homestudio-mcp`. See
[mcp/README.md](mcp/README.md).

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
`bluetooth: "unconfigured"`, `youtube_account: "unconfigured"` and
`cookies: "missing"` until they are done.

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
2. **Connect your YouTube account** (subscriptions, likes and playlists; read-only).
   Design: [docs/superpowers/specs/2026-11-01-youtube-device-signin.md](docs/superpowers/specs/2026-11-01-youtube-device-signin.md).
   1. In the [Google Cloud console](https://console.cloud.google.com/), in the same
      project as the API key (with **YouTube Data API v3** enabled), open
      **APIs & Services > OAuth consent screen**. Choose user type **External**, fill
      in the app name and your email, and add the scope
      `https://www.googleapis.com/auth/youtube.readonly`. Under **Audience**
      (publishing status) click **Publish app** so it is **In production**: in
      "Testing" the refresh token expires after 7 days. Google review is not
      needed for personal use; the app simply stays unverified.
   2. **APIs & Services > Credentials > Create credentials > OAuth client ID**,
      application type **TVs and Limited Input devices**. Keep the client ID and
      client secret at hand (never commit them).
   3. On the Windows PC run the hidden-input helper from your setup folder:
      `powershell -ExecutionPolicy Bypass -File set-youtube-oauth-client.ps1`. It
      asks for the client ID and the (hidden) client secret, writes them to
      `youtube: oauth_client_id / oauth_client_secret` in the Pi's
      `~/home-studio/config.local.yaml` (mode 600) and prints "Saved". Then restart:
      `systemctl --user restart home-studio`. `/api/status` now shows
      `youtube_account: "disconnected"`.
   4. In the app, open the **Taste** tab and tap **Connect YouTube**. It shows a
      code: open google.com/device on the phone (or any browser), enter the code,
      pick your account, click through "Google hasn't verified this app"
      (**Advanced > Go to ... (unsafe)**; it is your own app), and allow read-only
      access. The card switches to "Connected as <channel>", and the Pi imports
      the account right away and seeds or refreshes the taste profile. The token
      lives in `~/home-studio/secrets/youtube_oauth.json` (mode 600);
      **Disconnect** revokes and deletes it.
3. **YouTube cookies (optional with a connected account).** They add watch
   history, YouTube's home recommendations and Watch Later (plus Premium audio and
   bot-check cover). In a desktop browser logged into YouTube, export
   `cookies.txt` with a "Get cookies.txt LOCALLY" type extension and copy it to
   `~/home-studio/secrets/cookies.txt` on the Pi (`chmod 600`). yt-dlp may create
   `secrets/cookies.txt` itself with anonymous cookies only (status shows
   `cookies: "missing"`); simply overwrite it with the exported file.
4. **Claude login.** On the Pi, run `claude` once and complete `/login`.
   (If `claude` is missing: `sudo npm i -g @anthropic-ai/claude-code`.)
5. **Import the account once** (not needed after connecting the account in the
   app, which imports right away). After the cookies are in place (and Claude is
   logged in, so the first taste profile can be seeded), run
   `cd ~/home-studio && .venv/bin/python -m src import-account`. After that the
   server re-imports daily at 04:00: over OAuth when the account is connected
   (cookie feeds only while `cookies` is `ok`), otherwise from cookies.
6. **A YouTube Data API key is strongly recommended** (`youtube: api_key:` in
   `config.local.yaml`). Without one every search and metadata lookup goes
   through yt-dlp, paced 2 s apart, which can make the morning build miss its
   few-minute window.
7. **Tailscale** for reaching the app away from home:
   `curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`,
   then note the MagicDNS name and use it in the app.
8. `systemctl --user restart home-studio`, then
   `curl -s localhost:8765/api/status` should show `ok: true`, bluetooth
   `connected`, `youtube_account: "connected"` and (if you added them) cookies `ok`.
