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
| 409 | The player rejected the command (`error` is mpv's message), or a YouTube sign-in request that does not fit the current state (not set up, already connected) |
| 422 | Validation failed. `error` names each bad field: `"<field>: <message>"`, joined by `"; "`, e.g. `"satisfaction: Input should be less than or equal to 10"` |
| 500 | Unexpected server error: `error` is always `"internal error"` (details are in the server log) |
| 502 | YouTube search failed, or Google refused / could not be reached for sign-in |
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
  "kind": "user",
  "play": "now",
  "status": "done",
  "summary": "Queued two deep dives on GPU memory",
  "error": null,
  "items": [{ "...": "QueueItemOut" }],
  "created_at": "2026-11-01T14:00:00+00:00",
  "finished_at": "2026-11-01T14:01:10+00:00"
}
```

`kind` is `"user"` (asked through the API) or `"morning"` (the morning set, scheduled or
`/api/morning/test`). `play` is `"now"` or `"end"`, as requested.
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
  "count": 3,
  "build_lead_min": 5,
  "next_build_at": "2026-11-02T07:40:00-06:00",
  "last_outcome": {"date": "2026-11-01", "built": true, "fallback": false, "started": true,
                   "request_id": 14, "status": "done"}
}
```

`days` are weekdays, `0` = Monday … `6` = Sunday. The agent builds the set `build_lead_min`
minutes before `wake_time` (the build may start any time in the 3 minutes after that) and
playback starts at `wake_time`.

`next_build_at` and `last_outcome` are read-only (sent back, ignored on PUT):

- `next_build_at`: when the next build starts, ISO 8601 in the station's local time zone with its
  offset; `null` when the morning is disabled or no day is selected. Today's build counts until its
  window has passed or it has run.
- `last_outcome`: the latest morning request, or `null` if there has never been one. `date` is its
  local date; `built` is `true` when the agent's picks were queued (`status` `"done"`); `fallback`
  is `true` when the fallback set (unplayed imports, then favourites) was queued instead or as well;
  `started` is `true` when the scheduled start ran that day (a manual `/api/morning/test` does not
  set it); `request_id` and `status` are the request's (poll `/api/agent/requests/{id}` for more).

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

Pause or resume. When idle, starts the queue from its first item (like `next`); does nothing
when idle with an empty queue. No body.

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

Rate a video. Each POST creates a new rating; the displayed rating (in history) is the latest
one, not merged with earlier ones: fields you leave out are `null` in the new rating, not carried
over. To change only the notes, send `liked` and `satisfaction` again too. At least one of `liked`,
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
the background by Claude. Poll GET `/api/taste` until `updated_at` changes, and watch
`/api/status` `claude`: if it is not `"ok"` the refresh failed (the correction stays saved and is
applied on the next successful refresh). Give up after about 60 s.

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

## YouTube account

Sign-in to the listener's YouTube account with Google's device flow: the Pi gets a short
code, the listener enters it at google.com/device on any phone or PC, and the server polls
Google in the background until it is approved. The server only asks for read-only access
(`youtube.readonly`). Cookies stay optional: they add watch history, recommendations and
Watch Later.

**YouTubeAccount** (every key is always present, `null` when it does not apply):

```json
{
  "state": "pending",
  "user_code": "ABCD-EFGH",
  "verification_url": "https://www.google.com/device",
  "expires_at": "2026-11-01T15:30:00+00:00",
  "error": null,
  "channel_title": null
}
```

| `state` | Meaning | Fields set |
|---|---|---|
| `"unconfigured"` | No OAuth client ID/secret on the Pi: "YouTube sign-in isn't set up on the Pi yet." | none |
| `"disconnected"` | Set up, not signed in | none |
| `"pending"` | A code is waiting to be entered | `user_code`, `verification_url`, `expires_at` |
| `"connected"` | Signed in | `channel_title` (may be `null` if YouTube could not be asked; cached, 1 quota unit the first time) |
| `"error"` | The last sign-in failed (denied, code expired, Google refused) | `error`, a message to show as is |

A pending code that passes `expires_at` turns into `"error"` (`"the code expired before it
was entered; try again"`). If Google later rejects the saved sign-in (revoked in the Google
account, or expired), the server deletes it, the state goes back to `"disconnected"`, and a
`youtube` status event with a message starting `OAuthRevoked:` is logged.

Right after a successful sign-in the server runs an account import in the background
(subscriptions, likes and playlists), and seeds or refreshes the taste profile from it.

### GET /api/youtube/oauth

The current sign-in state. Poll it every few seconds while `state` is `"pending"`.

Response `200`:

```json
{"ok": true, "data": {"state": "connected", "user_code": null, "verification_url": null, "expires_at": null, "error": null, "channel_title": "Jamil's Channel"}, "error": null}
```

```json
{"ok": true, "data": {"state": "unconfigured", "user_code": null, "verification_url": null, "expires_at": null, "error": null, "channel_title": null}, "error": null}
```

### POST /api/youtube/oauth/start

Start a sign-in: asks Google for a code and starts polling for approval. No body. While a
code is already pending, returns that same code (no new request to Google). Allowed from
`"disconnected"` and `"error"`.

Response `200`:

```json
{"ok": true, "data": {"state": "pending", "user_code": "ABCD-EFGH", "verification_url": "https://www.google.com/device", "expires_at": "2026-11-01T15:30:00+00:00", "interval_s": 5}, "error": null}
```

`interval_s` is how often the server polls Google; the app can poll `GET /api/youtube/oauth`
at its own pace (every 3 s is fine).

Errors:

```json
{"ok": false, "data": null, "error": "YouTube sign-in isn't set up on the Pi yet"}
```

- `409` `"YouTube sign-in isn't set up on the Pi yet"` (state `"unconfigured"`)
- `409` `"already connected; disconnect first"`
- `502` `"could not reach Google"` or `"Google refused the sign-in request: <code>"`

### POST /api/youtube/oauth

Same as `POST /api/youtube/oauth/start` (alias).

### DELETE /api/youtube/oauth

Disconnect: cancels a pending code, revokes the token at Google (best effort), deletes it
from the Pi, and returns the new state (`"disconnected"`, or `"unconfigured"` when sign-in
is not set up). Safe to call in any state.

Response `200`:

```json
{"ok": true, "data": {"state": "disconnected", "user_code": null, "verification_url": null, "expires_at": null, "error": null, "channel_title": null}, "error": null}
```

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
{"ok": true, "data": {"id": 9, "prompt": "something about GPU memory", "kind": "user", "play": "now", "status": "running", "summary": null, "error": null, "items": [], "created_at": "2026-11-01T14:00:00+00:00", "finished_at": null}, "error": null}
```

Errors: `404` `"request not found"`.

### GET /api/agent/requests

Recent requests, newest first (user and morning requests).

Query: `limit` (1–100, default 20). Example: `/api/agent/requests?limit=10`.

Response `200`:

```json
{"ok": true, "data": {"requests": [{"id": 9, "prompt": "something about GPU memory", "kind": "user", "play": "now", "status": "done", "summary": "Queued two deep dives", "error": null, "items": [], "created_at": "2026-11-01T14:00:00+00:00", "finished_at": "2026-11-01T14:01:10+00:00"}]}, "error": null}
```

Errors: `422` for an out-of-range `limit`.

## Morning

### GET /api/morning

Response `200`: MorningSettings.

```json
{"ok": true, "data": {"enabled": true, "wake_time": "07:45", "days": [0, 1, 2, 3, 4, 5, 6], "topics": ["technology", "AI", "hardware", "engineering", "news/politics"], "count": 3, "build_lead_min": 5, "next_build_at": "2026-11-02T07:40:00-06:00", "last_outcome": null}, "error": null}
```

### PUT /api/morning

Replace the morning settings. All fields are required except `build_lead_min` (left out: the
current value is kept).

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
| `build_lead_min` | int | optional, 1–60 (default 5): minutes before `wake_time` the build starts |

Response `200`: the saved MorningSettings (with `next_build_at` and `last_outcome`).

Errors: `422`, e.g. `"wake_time: String should match pattern '^\d{2}:\d{2}$'"`,
`"wake_time must be HH:MM (24-hour)"`, `"topics must not be empty"`, `"count: Input should be less than or equal to 6"`.

### POST /api/morning/test

Build a morning set now and start playback unless something is actively playing. A paused item
is stopped and goes back into the queue behind the morning items; something playing is left alone
(the set is still queued). If the agent fails, the fallback set is queued instead. No body. Poll
GET `/api/agent/requests/{id}` with the returned id.

The scheduled morning works the same way at `wake_time`: if the start fails (for example mpv is
restarting) it is retried every 20 s for 10 minutes.

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
    "youtube_account": "connected",
    "claude": "ok",
    "source": "ok",
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
| `cookies` | `"ok"`; `"missing"` (no cookies file, or a file with no non-expired YouTube sign-in cookies, i.e. "not signed in"; yt-dlp and mpv create anonymous-only files themselves); `"expired"` (YouTube's last answer was a sign-in / "not a bot" check, made after the cookies file was last written; re-export the cookies and it clears, as does the next successful account import) |
| `youtube_account` | `"unconfigured"`, `"disconnected"`, `"pending"`, `"connected"` or `"error"`: the `state` of `GET /api/youtube/oauth` (no channel title here) |
| `claude` | `"ok"`, `"rate_limited"`, `"not_logged_in"` (run `claude login` on the Pi), `"missing"` (CLI not installed), `"unknown"` (no agent run yet) |
| `source` | `"ok"`, or `"broken"` after 3 playback errors in a row (the player stopped advancing so the queue is not drained; the failing item is back at the front). Clears on the next successful play. Usually stale yt-dlp or cookies |
| `ytdlp_version` | installed yt-dlp version string |
| `quota_used_today` | YouTube Data API units used today (the quota day is the Pacific-time date) |
| `quota_ceiling` | the server stops using the Data API at this many units |
| `events` | the 20 most recent status events, newest first; `level` is `"info"`, `"warn"` or `"error"` |
