# Home Studio Server

The backend of a personal "listening station": a small Raspberry Pi server that plays YouTube audio to a Bluetooth speaker (through a USB Bluetooth adapter) and builds AI-curated morning sets. It exposes a REST API for a companion Android app (see the `home-studio` repository), keeps ratings, listening history and a taste profile in SQLite, and uses the `claude` CLI to turn your YouTube account data into a daily set of videos.

Design: [docs/superpowers/specs/2026-11-01-listening-station-design.md](docs/superpowers/specs/2026-11-01-listening-station-design.md). API reference: [docs/api.md](docs/api.md).

## Stack

- Python 3.11+, FastAPI and uvicorn (REST API, default port 8765), SQLite with SQL migrations
- `yt-dlp` and `mpv` for playback, the YouTube Data API and OAuth device sign-in for account data
- BlueZ over D-Bus for Bluetooth speaker pairing and reconnect
- Claude CLI (`claude`, run without tools or MCP servers) for taste profiling and morning-set curation
- systemd user units for the service and a `yt-dlp` update timer
- A read-only MCP server in `mcp/` (Python, `uv`, Docker image) for assistants
- pytest, ruff, black; GitHub Actions CI

## Running

```
pip install -e .[dev]
python -m pytest -q
home-studio            # or: python -m src   (API on :8765)
```

To deploy to a Pi, `scripts/deploy-pi3.sh` runs `scripts/setup-pi3.sh` on first use (packages, venv, systemd units), then syncs the code and restarts the service (never `secrets/`, the database or `config.local.yaml`). Run the deploy from a Unix shell and keep the target out of the repo:

```
HOME_STUDIO_HOST=user@<pi-host> bash scripts/deploy-pi3.sh
```

## Configuration

`config.yaml` holds defaults. Copy `config.example.local.yaml` to `config.local.yaml` (git-ignored) for secrets.

- `youtube.api_key` (strongly recommended; otherwise searches go through `yt-dlp`, paced 2 s apart)
- `youtube.oauth_client_id` / `youtube.oauth_client_secret` for account sign-in with the device-code flow; the token is written to `youtube.oauth_token_path` (default `secrets/youtube_oauth.json`)
- `bluetooth.speaker_mac` and `bluetooth.adapter` (default `hci1`)
- Environment overrides: `HOME_STUDIO_YOUTUBE_API_KEY`, `HOME_STUDIO_BT_SPEAKER_MAC`, `HOME_STUDIO_COOKIES_PATH`
- Cookies live in `secrets/` and the database in `data/`; both are git-ignored

## Finishing setup on the Pi

The server runs without these steps but reports `bluetooth: "unconfigured"`, `youtube_account: "unconfigured"` and `cookies: "missing"` in `/api/status` until they are done.

1. **Pair the speaker.** Put it in pairing mode, then run `bash scripts/pair-bluetooth.sh --find` (read-only scan) and `bash scripts/pair-bluetooth.sh <SPEAKER_MAC> hci1`. Put the MAC under `bluetooth: speaker_mac:` in `config.local.yaml`; the service powers the adapter on and reconnects at start.
2. **Connect a YouTube account** (read-only). Create an OAuth client of type "TVs and Limited Input devices" in a Google Cloud project with YouTube Data API v3 enabled and the `youtube.readonly` scope, put the client ID and secret in `config.local.yaml`, restart the service, then use **Connect YouTube** in the app's Taste tab. Details: [docs/superpowers/specs/2026-11-01-youtube-device-signin.md](docs/superpowers/specs/2026-11-01-youtube-device-signin.md).
3. **Cookies (optional).** Export `cookies.txt` from a logged-in browser to `secrets/cookies.txt` to add watch history, home recommendations and Watch Later.
4. **Claude login.** Run `claude` once on the Pi and complete `/login`.
5. **Remote access.** The API has no authentication and is meant to be reached only over a private network such as a VPN or tailnet.
6. Restart with `systemctl --user restart home-studio`; `curl -s localhost:8765/api/status` should then report `ok: true`.

## MCP server

`mcp/` holds `homestudio-mcp`, a read-only MCP server over this API (taste, ratings, listening history, now playing, queue). It runs beside an assistant gateway, not on the Pi. See [mcp/README.md](mcp/README.md).

## Project layout

| Path | What |
|------|------|
| `src/api/` | FastAPI routes (player, queue, library, agent, YouTube, system) |
| `src/player/` | mpv engine and supervisor |
| `src/agent/` | Claude CLI wrapper, taste profile, morning-set builder |
| `src/sources/` | YouTube Data API, OAuth, yt-dlp and catalog sources |
| `src/store/`, `src/db/` | SQLite stores and migrations |
| `scripts/`, `systemd/` | Pi setup, deploy, Bluetooth pairing, service units |
| `mcp/` | read-only MCP server |
