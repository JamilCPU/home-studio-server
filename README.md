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
