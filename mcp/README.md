# homestudio-mcp

A read-only [MCP](https://modelcontextprotocol.io) server over the Home Studio REST API
(`docs/api.md`). It lets an assistant answer questions such as "what did I listen to this
week?" or "what is playing?" without being able to change anything: it only calls `GET`
endpoints.

It runs beside the gateway, not on the Pi. The gateway reaches it over streamable HTTP at
`/mcp` and sends the shared secret in the `X-Gateway-Secret` header; requests without it get
`401 {"error": "unauthorized"}`. `GET /healthz` is open and returns `{"ok": true}`.

## Tools

| Tool | API call | Returns |
|---|---|---|
| `get_taste_profile` | `GET /api/taste` | summary, likes, dislikes (or `null`) |
| `list_ratings(limit=50, since=None)` | `GET /api/ratings` | every rating, newest first |
| `get_listening_history(since=None, limit=50)` | `GET /api/history` | plays, newest first, without chapters or thumbnails |
| `get_now_playing` | `GET /api/now` | state, title, channel, position, duration, output device |
| `list_queue` | `GET /api/queue` | upcoming items and total remaining seconds |

`limit` is 1 to 200. `since` is an ISO 8601 timestamp and is only sent when given.

## Configuration

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `HOMESTUDIO_API_URL` | yes | | Base URL of the Home Studio API |
| `HOMESTUDIO_MCP_SHARED_SECRET` | yes | | Value the gateway sends in `X-Gateway-Secret` |
| `HOMESTUDIO_MCP_HOST` | no | `0.0.0.0` | Bind address |
| `HOMESTUDIO_MCP_PORT` | no | `8000` | Bind port |

## Run locally

    cd mcp
    uv sync
    HOMESTUDIO_API_URL=http://localhost:8765 HOMESTUDIO_MCP_SHARED_SECRET=dev-secret uv run homestudio-mcp

## Develop

    uv run pytest -q
    uv run ruff check .

CI builds the image and pushes `ghcr.io/jamilcpu/homestudio-mcp:latest` (and `:<commit sha>`)
on every push to `master`.
