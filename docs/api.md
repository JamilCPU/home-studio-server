# Home Studio REST API

The Home Studio server runs on the Pi and is reached by the Android app over Tailscale.

- **Base URL:** `http://<pi-tailscale-name>:8765`
- Every path starts with `/api`.
- Request bodies are JSON (`Content-Type: application/json`). Unknown body fields are ignored.
  String fields have leading and trailing whitespace stripped before validation.
- There is no authentication: access is limited to the tailnet.
- Timestamps are ISO 8601 strings in UTC as stored, e.g. `"2026-11-01T14:03:22.512345+00:00"`.
- Durations and positions are seconds. `duration_s` on a video is an integer or `null` (unknown);
  `position_s` / `duration_s` in now-playing are floats.

## Envelope

Every response, success or error, has the same shape:

```json
{"ok": true, "data": {"...": "..."}, "error": null}
```

```json
{"ok": false, "data": null, "error": "video not found"}
```

`data` is `null` on every error. `error` is a human-readable string; show it to the user as is.

## Error codes

| Status | Meaning |
|---|---|
| 200 | OK |
| 201 | Created (queue item, rating) |
| 202 | Accepted: work continues in the background (agent request, taste correction, morning test) |
| 400 | Bad request the server understood but refuses (bad link, empty search, wrong reorder ids) |
| 404 | Not found (unknown route, video, queue item, request or trait) |
| 405 | Method not allowed on this path |
| 409 | The player rejected the command (`error` is mpv's message) |
| 422 | Validation failed. `error` names each bad field: `"<field>: <message>"`, joined by `"; "`, e.g. `"satisfaction: Input should be less than or equal to 10"` |
| 500 | Unexpected server error: `error` is always `"internal error"` (details are in the server log) |
| 502 | YouTube search failed |
| 503 | `"player unavailable"` (mpv is down) or `"YouTube sign-in required (cookies expired)"` |

## Shared shapes

**VideoOut**

```json
{
  "video_id": "dQw4w9WgXcQ",
  "title": "How GPUs Work",
  "channel": "Some Channel",
  "thumbnail_url": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
  "duration_s": 1834,
  "chapters": [{"title": "Intro", "start_s": 0.0}, {"title": "Memory", "start_s": 312.0}]
}
```

`channel`, `thumbnail_url` and `duration_s` may be `null`. `chapters` may be `[]`.

**QueueItemOut**: VideoOut plus the queue fields.

```json
{
  "id": 42,
  "video_id": "dQw4w9WgXcQ",
  "title": "How GPUs Work",
  "channel": "Some Channel",
  "thumbnail_url": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
  "duration_s": 1834,
  "source": "agent",
  "reason": "Deep dive on hardware, matches your taste",
  "chapters": [{"title": "Intro", "start_s": 0.0}]
}
```

`source` is one of `"agent"`, `"user"`, `"morning"`, `"fallback"`. `reason` may be `null`.

**NowPlaying**

```json
{
  "state": "playing",
  "item": { "...": "QueueItemOut" },
  "position_s": 125.4,
  "duration_s": 1834.0,
  "output_device": "Echo"
}
```

`state` is `"playing"`, `"paused"` or `"idle"`. When idle: `item` is `null`, `position_s` is `0.0`
and `duration_s` is `null`. `output_device` is the configured speaker label.

**HistoryOut**

```json
{
  "play_id": 17,
  "video": { "...": "VideoOut" },
  "started_at": "2026-11-01T14:03:22.512345+00:00",
  "seconds_listened": 912.5,
  "end_reason": "skipped",
  "source": "agent",
  "rating": {"liked": true, "satisfaction": 8, "notes": "great", "created_at": "2026-11-01T14:20:00+00:00"}
}
```

`end_reason` is `"finished"`, `"skipped"`, `"stopped"`, `"error"` or `null` (still playing).
`seconds_listened` and `source` may be `null`. Ratings are per video: `rating` is the video's
latest rating, or `null` if it was never rated. Inside `rating`, any of `liked`, `satisfaction`
and `notes` may be `null`.

**Taste**

```json
{
  "summary": "Prefers long-form engineering deep dives",
  "likes": ["hardware teardowns", "long-form"],
  "dislikes": ["clickbait"],
  "updated_at": "2026-11-01T09:00:00+00:00"
}
```

**AgentRequestOut**

```json
{
  "id": 9,
  "prompt": "something about GPU memory",
  "status": "done",
  "summary": "Queued two deep dives on GPU memory",
  "error": null,
  "items": [{ "...": "QueueItemOut" }],
  "created_at": "2026-11-01T14:00:00+00:00",
  "finished_at": "2026-11-01T14:01:10+00:00"
}
```

`status` is `"pending"`, `"running"`, `"done"` or `"failed"`. On `"failed"`, `error` holds a
readable reason. `summary`, `error` and `finished_at` are `null` until set. `items` are every queue
item the request added, in queue order (including ones already played or removed).

**MorningSettings**

```json
{
  "enabled": true,
  "wake_time": "07:45",
  "days": [0, 1, 2, 3, 4, 5, 6],
  "topics": ["technology", "AI", "hardware", "engineering", "news/politics"],
  "count": 3
}
```

`days` are weekdays, `0` = Monday … `6` = Sunday.

---

## Player

All player endpoints may fail with **503** `"player unavailable"` when mpv is down, or **409** with
mpv's message when it rejects a command.

### GET /api/now

What is playing.

Response `200`:

```json
{"ok": true, "data": {"state": "idle", "item": null, "position_s": 0.0, "duration_s": null, "output_device": "Echo"}, "error": null}
```

### POST /api/player/toggle

Pause or resume. Does nothing when idle. No body.

Response `200`: NowPlaying.

```json
{"ok": true, "data": {"state": "paused", "item": {"id": 42, "video_id": "dQw4w9WgXcQ", "title": "How GPUs Work", "channel": "Some Channel", "thumbnail_url": null, "duration_s": 1834, "source": "user", "reason": null, "chapters": []}, "position_s": 125.4, "duration_s": 1834.0, "output_device": "Echo"}, "error": null}
```

### POST /api/player/next

Skip to the next queued item (starts playback if idle). When the queue is empty, playback stops
and the state becomes `"idle"`. No body.

Response `200`: NowPlaying.

### POST /api/player/previous

Restart the current item if more than 5 s in, otherwise go back to the previously played item.
No body.

Response `200`: NowPlaying.

### POST /api/player/stop

Stop playback. The current item goes back to the front of the queue. No body.

Response `200`: NowPlaying (state `"idle"`).

### POST /api/player/seek

Seek to an absolute position. Out-of-range values are clamped to `[0, duration - 1]`, never an error.

Request:

```json
{"position_s": 300.0}
```

Response `200`: NowPlaying.

Errors: `422` if `position_s` is missing or not a number.

### POST /api/player/skip

Jump relative to the current position (negative goes back). Clamped like seek.

Request:

```json
{"delta_s": -15}
```

Response `200`: NowPlaying.

Errors: `422` if `delta_s` is missing or not a number.

## Queue

### GET /api/queue

Upcoming items (not including the one playing), in play order, and their total length.

Response `200`:

```json
{"ok": true, "data": {"items": [{"id": 42, "video_id": "dQw4w9WgXcQ", "title": "How GPUs Work", "channel": "Some Channel", "thumbnail_url": null, "duration_s": 1834, "source": "user", "reason": null, "chapters": []}], "remaining_s": 1834}, "error": null}
```

`remaining_s` is an integer; videos with unknown duration count as 0.

### POST /api/queue

Add a video by id or by YouTube link. Exactly one of `video_id` or `url` is required. `where`
is required: `"next"` (right after the current item) or `"end"`. Adding does not start playback.

Request:

```json
{"url": "https://youtu.be/dQw4w9WgXcQ", "where": "next"}
```

```json
{"video_id": "dQw4w9WgXcQ", "where": "end"}
```

Accepted links: `youtube.com/watch?v=<id>` (also `www.`, `m.`, `music.`), `youtu.be/<id>` and
`youtube.com/shorts/<id>`, with or without `https://`. Extra query parameters are ignored.

Response `201`: the new QueueItemOut.

```json
{"ok": true, "data": {"id": 43, "video_id": "dQw4w9WgXcQ", "title": "How GPUs Work", "channel": "Some Channel", "thumbnail_url": null, "duration_s": 1834, "source": "user", "reason": null, "chapters": []}, "error": null}
```

Errors:
- `400` `"give exactly one of video_id or url"`.
- `400` `"not a YouTube video link"`: any other link.
- `404` `"video not found"`: YouTube has no such video.
- `422`: `where` missing or not `"next"`/`"end"`.
- `503` `"YouTube sign-in required (cookies expired)"`.

### DELETE /api/queue/{id}

Remove one upcoming item by its queue item `id`.

Response `200`:

```json
{"ok": true, "data": {"removed": 1}, "error": null}
```

Errors: `404` `"queue item not found"` if `id` is not an upcoming item (including the one playing);
`422` if `id` is not an integer.

### DELETE /api/queue

Remove every upcoming item. The playing item keeps playing.

Response `200`:

```json
{"ok": true, "data": {"removed": 3}, "error": null}
```

### PUT /api/queue/order

Set the order of the upcoming items. `ids` must be exactly the current upcoming ids, each once.

Request:

```json
{"ids": [44, 42, 43]}
```

Response `200`:

```json
{"ok": true, "data": {"items": [{"id": 44, "...": "QueueItemOut"}, {"id": 42, "...": "QueueItemOut"}, {"id": 43, "...": "QueueItemOut"}]}, "error": null}
```

Errors: `400` `"ids must match the set of queued item ids"`; `422` if `ids` is missing or not a
list of integers.

### GET /api/search

Search YouTube. Uses the YouTube Data API when configured and within quota, otherwise yt-dlp.
Returns up to 20 results.

Query: `q` (required, 1–200 characters after trimming). Example: `/api/search?q=gpu%20memory`.

Response `200`:

```json
{"ok": true, "data": {"results": [{"video_id": "dQw4w9WgXcQ", "title": "How GPUs Work", "channel": "Some Channel", "thumbnail_url": null, "duration_s": 1834, "chapters": []}]}, "error": null}
```

Errors: `400` `"q must not be empty"` / `"q must be at most 200 characters"`; `502` `"search failed"`;
`503` `"YouTube sign-in required (cookies expired)"`.

## Ratings and history

### POST /api/ratings

Rate a video. Every rating is kept; history shows the latest. At least one of `liked`,
`satisfaction` or `notes` is required. The video must be known to the server (played, queued or
searched).

Request:

```json
{"video_id": "dQw4w9WgXcQ", "liked": true, "satisfaction": 8, "notes": "loved the memory section"}
```

| Field | Type | Rules |
|---|---|---|
| `video_id` | string | required |
| `liked` | bool | optional |
| `satisfaction` | int | optional, 1–10 |
| `notes` | string | optional, at most 2000 characters |

Response `201`:

```json
{"ok": true, "data": {"id": 12}, "error": null}
```

Errors: `404` `"video not found"`; `422` e.g. `"satisfaction: Input should be less than or equal to 10"`,
`"notes: String should have at most 2000 characters"`, or `"a rating needs liked, satisfaction or notes"`.

### GET /api/history

Plays, newest first.

Query: `unrated` (bool, default `false`: only plays whose video has no rating), `limit` (1–200,
default 50), `offset` (≥ 0, default 0). Example: `/api/history?unrated=true&limit=20&offset=0`.

Response `200`:

```json
{"ok": true, "data": {"entries": [{"play_id": 17, "video": {"video_id": "dQw4w9WgXcQ", "title": "How GPUs Work", "channel": "Some Channel", "thumbnail_url": null, "duration_s": 1834, "chapters": []}, "started_at": "2026-11-01T14:03:22.512345+00:00", "seconds_listened": 912.5, "end_reason": "skipped", "source": "agent", "rating": null}]}, "error": null}
```

Errors: `422` for an out-of-range `limit` or `offset`.

## Taste

### GET /api/taste

The current taste profile, or `null` if none exists yet.

Response `200`:

```json
{"ok": true, "data": {"summary": "Prefers long-form engineering deep dives", "likes": ["hardware teardowns"], "dislikes": ["clickbait"], "updated_at": "2026-11-01T09:00:00+00:00"}, "error": null}
```

```json
{"ok": true, "data": null, "error": null}
```

### POST /api/taste/corrections

Tell the agent something about your taste in plain words. Saved now; the profile is refreshed in
the background (poll GET `/api/taste`).

Request:

```json
{"text": "less politics, more hardware"}
```

`text`: required, 1–1000 characters.

Response `202`:

```json
{"ok": true, "data": {"id": 5}, "error": null}
```

Errors: `422` if `text` is empty or too long.

### DELETE /api/taste/traits

Remove one like or dislike from the profile (the text must match exactly). This request has a JSON
body.

Request:

```json
{"kind": "likes", "text": "hardware teardowns"}
```

`kind`: `"likes"` or `"dislikes"`. `text`: required, 1–1000 characters.

Response `200`: the updated Taste.

```json
{"ok": true, "data": {"summary": "Prefers long-form engineering deep dives", "likes": [], "dislikes": ["clickbait"], "updated_at": "2026-11-01T15:00:00+00:00"}, "error": null}
```

Errors: `404` `"no taste profile yet"` or `"'hardware teardowns' is not in likes"`; `422` for a bad
`kind`.

## Agent

### POST /api/agent/requests

Ask the agent to find something. Runs in the background; poll GET `/api/agent/requests/{id}`.

Request:

```json
{"prompt": "something about GPU memory", "play": "now"}
```

`prompt`: required, 1–1000 characters. `play`: required, `"now"` (play its picks next, right away)
or `"end"` (append to the queue).

Response `202`:

```json
{"ok": true, "data": {"id": 9}, "error": null}
```

Errors: `422` for an empty/too-long `prompt` or a bad `play`.

### GET /api/agent/requests/{id}

One request with the items it queued.

Response `200`: AgentRequestOut.

```json
{"ok": true, "data": {"id": 9, "prompt": "something about GPU memory", "status": "running", "summary": null, "error": null, "items": [], "created_at": "2026-11-01T14:00:00+00:00", "finished_at": null}, "error": null}
```

Errors: `404` `"request not found"`.

### GET /api/agent/requests

Recent requests, newest first (user and morning requests).

Query: `limit` (1–100, default 20). Example: `/api/agent/requests?limit=10`.

Response `200`:

```json
{"ok": true, "data": {"requests": [{"id": 9, "prompt": "something about GPU memory", "status": "done", "summary": "Queued two deep dives", "error": null, "items": [], "created_at": "2026-11-01T14:00:00+00:00", "finished_at": "2026-11-01T14:01:10+00:00"}]}, "error": null}
```

Errors: `422` for an out-of-range `limit`.

## Morning

### GET /api/morning

Response `200`: MorningSettings.

```json
{"ok": true, "data": {"enabled": true, "wake_time": "07:45", "days": [0, 1, 2, 3, 4, 5, 6], "topics": ["technology", "AI", "hardware", "engineering", "news/politics"], "count": 3}, "error": null}
```

### PUT /api/morning

Replace the morning settings. All fields are required.

Request:

```json
{"enabled": true, "wake_time": "06:30", "days": [0, 1, 2, 3, 4], "topics": ["AI", "hardware"], "count": 2}
```

| Field | Type | Rules |
|---|---|---|
| `enabled` | bool | |
| `wake_time` | string | `"HH:MM"`, 24-hour, `00:00`–`23:59` |
| `days` | int[] | each 0–6, at most 7 |
| `topics` | string[] | at least 1, at most 20, each 1–100 characters |
| `count` | int | 1–6 |

Response `200`: the saved MorningSettings.

Errors: `422`, e.g. `"wake_time: String should match pattern '^\d{2}:\d{2}$'"`,
`"wake_time must be HH:MM (24-hour)"`, `"topics must not be empty"`, `"count: Input should be less than or equal to 6"`.

### POST /api/morning/test

Build and start a morning set right now. No body. Poll GET `/api/agent/requests/{id}` with the
returned id.

Response `202`:

```json
{"ok": true, "data": {"request_id": 14}, "error": null}
```

## System

### GET /api/status

Health of every part of the station.

Response `200`:

```json
{
  "ok": true,
  "data": {
    "player": "playing",
    "bluetooth": "connected",
    "cookies": "ok",
    "claude": "ok",
    "ytdlp_version": "2026.03.17",
    "quota_used_today": 300,
    "quota_ceiling": 9500,
    "events": [
      {"component": "bluetooth", "level": "info", "message": "bluetooth connected", "created_at": "2026-11-01T07:40:00+00:00"}
    ]
  },
  "error": null
}
```

| Field | Values |
|---|---|
| `player` | `"playing"`, `"paused"`, `"idle"`, or `"unavailable"` when mpv is down |
| `bluetooth` | `"connected"`, `"disconnected"`, `"unconfigured"` (no speaker set) |
| `cookies` | `"ok"`; `"missing"` (no cookies file); `"expired"` (YouTube's last answer was a sign-in / "not a bot" check; re-export cookies) |
| `claude` | `"ok"`, `"rate_limited"`, `"not_logged_in"` (run `claude login` on the Pi), `"missing"` (CLI not installed), `"unknown"` (no agent run yet) |
| `ytdlp_version` | installed yt-dlp version string |
| `quota_used_today` | YouTube Data API units used today (the quota day is the Pacific-time date) |
| `quota_ceiling` | the server stops using the Data API at this many units |
| `events` | the 20 most recent status events, newest first; `level` is `"info"`, `"warn"` or `"error"` |
