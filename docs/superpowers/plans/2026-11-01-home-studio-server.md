# Home Studio Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn YoutubeBrainwash into the Home Studio Pi server: SQLite history/ratings, mpv playback to the Echo over Bluetooth with seek, hybrid YouTube Data API + yt-dlp sources, a `claude`-CLI agent (on-request + morning set), and the REST API the Android app uses.

**Architecture:** One Python process on Pi3 (FastAPI + uvicorn) owns a SQLite DB, an mpv child process (JSON IPC), a playback engine thread, an agent worker thread, and an in-process morning scheduler. Sources (Data API, yt-dlp) and the Claude CLI sit behind small interfaces so tests use fakes. The phone polls the API over Tailscale.

**Tech Stack:** Python ≥3.11 (dev PC has 3.13; Pi OS bookworm ships 3.11, so no 3.12+ syntax), stdlib `sqlite3`, FastAPI, uvicorn, `google-api-python-client` (API key only), `yt_dlp` Python API, mpv, BlueZ/`bluetoothctl`, pytest.

**Spec:** `docs/superpowers/specs/2026-11-01-listening-station-design.md` (source of truth; read it first).

**Repo:** `JamilCPU/home-studio-server`, local `F:\Dev\YoutubeBrainwash`. Work on a branch `feat/home-studio-server` off `master`. All commands run from the repo root.

## Global Constraints

- Repo is **public**: never commit cookies, API keys, Tailscale names, LAN IPs, the DB, or `config.local.yaml`. Secrets come from `config.local.yaml` or env vars prefixed `HOME_STUDIO_`.
- Agent runs only via the `claude` CLI (`claude -p <prompt>`) on Jamil's subscription. No Anthropic API key, no `anthropic` package.
- YouTube Data API uses an **API key only** (no OAuth). Free quota 10,000 units/day: `search.list` = 100 units, `videos.list` / `channels.list` / `playlistItems.list` = 1 unit per call (≤50 ids). Stop at a 9,500-unit safety ceiling.
- Never mark videos watched on YouTube (no `--mark-watched`).
- Never hard-code channel or creator names in prompts, defaults, config or tests.
- Morning: wake time default `07:45`, timezone `America/Chicago`, agent build starts 5 minutes before wake time, default count 2–3 (`count: 3`), default topics `["technology", "AI", "hardware", "engineering", "news/politics"]`, every day by default.
- Bluetooth output goes through the USB adapter **`hci1`** (Realtek BT 5.4), not the built-in `hci0`.
- When the queue empties, playback stops. No autoplay.
- Pi3 also runs ntopng: the service gets `Nice=5` and `MemoryMax=512M`.
- Every API response uses the envelope `{"ok": bool, "data": <payload|null>, "error": <string|null>}`.
- Immutable data: frozen dataclasses for records; return new objects instead of mutating.

## Review Focus

1. **Cookies expire or YouTube asks "confirm you're not a bot":** playback and imports must surface `cookies: "expired"` in `/api/status`, not hang or crash. (Task 7 test `test_bot_check_raises_source_auth_error`, Task 13 status test.)
2. **Claude CLI rate-limited / logged out / missing mid-request:** the agent request ends `failed` with a readable error and the morning set falls back. (Task 9 tests, Task 11 `test_fallback_used_when_agent_fails`.)
3. **Bluetooth speaker disconnected (Echo off, out of range):** status shows `bluetooth: "disconnected"`, the reconnect loop retries, playback calls don't raise. (Task 14 `test_status_reports_disconnected`.)
4. **Seek past the end / negative seek / skip beyond bounds:** clamp to `[0, duration-1]`, never error. (Task 5 `test_seek_is_clamped`.)
5. **Rating a video twice or rating with only notes:** every rating row is kept; history shows the latest. (Task 3 `test_multiple_ratings_kept_latest_shown`.)

---

## File Structure

```
src/
  config.py              # MODIFY: new fields + HOME_STUDIO_ env overrides
  models.py              # MODIFY: AppConfig fields; drop Snapcast fields
  db/__init__.py         # connect(), migrate()
  db/migrations/001_initial.sql
  store/videos.py        # videos, plays, ratings, history
  store/queue.py         # queue items
  store/agent.py         # agent requests, taste profile versions, corrections
  store/settings.py      # morning settings, api quota, status events, account imports
  player/mpv.py          # MpvIpc: talks to mpv's JSON IPC socket
  player/engine.py       # PlaybackEngine: queue → mpv, logs plays
  sources/data_api.py    # DataApi (API key, quota)
  sources/ytdlp.py       # YtDlp (audio URL, account feeds, metadata, transcripts, search)
  sources/catalog.py     # ensure_video / ensure_transcript caching
  agent/claude_cli.py    # run_claude(), extract_json(), errors
  agent/request.py       # run_request() pipeline
  agent/taste.py         # refresh_taste(), remove_trait()
  agent/morning.py       # morning_prompt(), fallback_morning()
  agent/worker.py        # AgentWorker thread (queue of request ids)
  scheduler.py           # MorningScheduler.tick(now)
  bluetooth.py           # is_connected(), ensure_connected()
  legacy.py              # migrate_legacy()
  api/app.py             # create_app(deps), envelope helpers
  api/routes_player.py, routes_queue.py, routes_library.py, routes_agent.py, routes_system.py
  __main__.py            # REWRITE: serve | migrate-legacy | import-account
docs/api.md              # API contract for the Android plan
systemd/home-studio.service, systemd/ytdlp-update.service, systemd/ytdlp-update.timer
scripts/deploy-pi3.sh, scripts/pair-bluetooth.sh (MODIFY), scripts/setup-pi3.sh
config.yaml (MODIFY), config.example.local.yaml (CREATE)
DELETE: src/api.py, src/player.py, src/analyzer.py, src/profile.py, src/playlist.py,
        scripts/setup-snapcast-*.sh, scripts/deploy-pi2.sh, scripts/install-cron.sh,
        and their tests. Keep src/auth.py, src/fetcher.py, src/transcript.py untouched for later OAuth work.
```

Tests mirror this under `tests/` (e.g. `tests/store/test_queue.py`). Shared fixture in `tests/conftest.py`: `db` → `migrate(connect(tmp_path / "t.db"))` connection.

---

### Task 1: Rebrand, config and branch

**Files:** Modify `pyproject.toml`, `src/models.py`, `src/config.py`, `config.yaml`, `README.md`, `.gitignore`; Create `config.example.local.yaml`; Test `tests/test_config.py`

**Interfaces:**
- Produces: `AppConfig` gains `db_path: str = "data/home_studio.db"`, `youtube_api_key: str = ""`, `cookies_path: str = "secrets/cookies.txt"`, `mpv_socket: str = "/tmp/home-studio-mpv.sock"`, `bt_adapter: str = "hci1"`, `bt_speaker_mac: str = ""`, `output_device_label: str = "Echo"`, `timezone: str = "America/Chicago"`, `claude_timeout_s: int = 300`, `quota_ceiling: int = 9500`. Removes `snapcast_*`, `snap_fifo`, `sample_rate`, `channels`, `max_feed_size`, `min_score`, `recency_hours`, `min/max_duration_minutes`, `search_topics`, `curation_time`, `default_wakeup_time`. `api_port` default `8765`.
- Env overrides: `HOME_STUDIO_YOUTUBE_API_KEY`, `HOME_STUDIO_BT_SPEAKER_MAC`, `HOME_STUDIO_COOKIES_PATH`.

- [ ] **Step 1:** `git checkout master && git pull && git checkout -b feat/home-studio-server`
- [ ] **Step 2: Failing tests** in `tests/test_config.py` (replace the old Snapcast/curation assertions):
  - `test_defaults` → `cfg.bt_adapter == "hci1"`, `cfg.timezone == "America/Chicago"`, `cfg.api_port == 8765`, `cfg.quota_ceiling == 9500`
  - `test_env_overrides_api_key` → with `HOME_STUDIO_YOUTUBE_API_KEY=k`, `cfg.youtube_api_key == "k"`
  - `test_local_yaml_merges` → `config.local.yaml` with `bluetooth: {speaker_mac: "AA:BB"}` gives `cfg.bt_speaker_mac == "AA:BB"`
- [ ] **Step 3:** Run `pytest tests/test_config.py -v`. Expect FAIL.
- [ ] **Step 4:** Implement the field changes and mapping (YAML sections `youtube.api_key`, `youtube.cookies_path`, `db.path`, `player.mpv_socket`, `bluetooth.adapter|speaker_mac|label`, `agent.claude_command|timeout_s`, `api.host|port`, `general.timezone`). `pyproject.toml` name → `home-studio-server`, description from the repo. README rewritten to describe Home Studio and link the spec. `.gitignore` adds `data/*.db*`, `secrets/`, `config.local.yaml`. `config.example.local.yaml` has placeholders only.
- [ ] **Step 5:** Run `pytest tests/test_config.py -v`. Expect PASS.
- [ ] **Step 6:** Commit `chore: rebrand to home-studio-server and new config`.

### Task 2: Database and migrations

**Files:** Create `src/db/__init__.py`, `src/db/migrations/001_initial.sql`; Test `tests/db/test_migrate.py`

**Interfaces:**
- Produces: `connect(path: Path) -> sqlite3.Connection` (creates parent dir, `row_factory=sqlite3.Row`, `PRAGMA foreign_keys=ON`, `journal_mode=WAL`, `check_same_thread=False`); `migrate(conn) -> int` applies `migrations/NNN_*.sql` with NNN greater than `schema_version.version`, in order, one transaction each, and returns the final version.

Schema (`001_initial.sql`). Exact columns and checks:
```sql
CREATE TABLE schema_version (version INTEGER NOT NULL);
CREATE TABLE videos (video_id TEXT PRIMARY KEY, title TEXT NOT NULL, channel_name TEXT, channel_id TEXT,
  description TEXT, duration_s INTEGER, published_at TEXT, tags_json TEXT, category TEXT, chapters_json TEXT,
  thumbnail_url TEXT, view_count INTEGER, like_count INTEGER, transcript TEXT, first_seen TEXT NOT NULL);
CREATE TABLE plays (id INTEGER PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos, queue_item_id INTEGER,
  started_at TEXT NOT NULL, ended_at TEXT, seconds_listened REAL,
  end_reason TEXT CHECK (end_reason IN ('finished','skipped','stopped','error')));
CREATE TABLE ratings (id INTEGER PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos,
  liked INTEGER CHECK (liked IN (0,1)), satisfaction INTEGER CHECK (satisfaction BETWEEN 1 AND 10),
  notes TEXT, created_at TEXT NOT NULL,
  CHECK (liked IS NOT NULL OR satisfaction IS NOT NULL OR notes IS NOT NULL));
CREATE TABLE agent_requests (id INTEGER PRIMARY KEY, prompt TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('user','morning')), play TEXT NOT NULL CHECK (play IN ('now','end')),
  status TEXT NOT NULL CHECK (status IN ('pending','running','done','failed')),
  summary TEXT, error TEXT, created_at TEXT NOT NULL, finished_at TEXT);
CREATE TABLE queue_items (id INTEGER PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos, position REAL NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('agent','user','morning','fallback')), reason TEXT,
  agent_request_id INTEGER REFERENCES agent_requests,
  status TEXT NOT NULL CHECK (status IN ('queued','playing','played','removed')), added_at TEXT NOT NULL);
CREATE TABLE taste_profiles (id INTEGER PRIMARY KEY, summary TEXT NOT NULL, likes_json TEXT NOT NULL,
  dislikes_json TEXT NOT NULL, source TEXT NOT NULL CHECK (source IN ('agent','user','legacy')), created_at TEXT NOT NULL);
CREATE TABLE taste_corrections (id INTEGER PRIMARY KEY, text TEXT NOT NULL, created_at TEXT NOT NULL, applied INTEGER NOT NULL DEFAULT 0);
CREATE TABLE morning_settings (id INTEGER PRIMARY KEY CHECK (id = 1), enabled INTEGER NOT NULL, wake_time TEXT NOT NULL,
  days_json TEXT NOT NULL, topics_json TEXT NOT NULL, count INTEGER NOT NULL);
INSERT INTO morning_settings VALUES (1, 1, '07:45', '[0,1,2,3,4,5,6]',
  '["technology","AI","hardware","engineering","news/politics"]', 3);
CREATE TABLE account_imports (id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('subscription','liked','playlist','history','recommended')),
  video_id TEXT, channel_id TEXT, title TEXT, imported_at TEXT NOT NULL);
CREATE TABLE api_quota (day TEXT PRIMARY KEY, units INTEGER NOT NULL);
CREATE TABLE status_events (id INTEGER PRIMARY KEY, component TEXT NOT NULL,
  level TEXT NOT NULL CHECK (level IN ('info','warn','error')), message TEXT NOT NULL, created_at TEXT NOT NULL);
```
All timestamps are UTC ISO-8601 strings. Days use Monday=0.

- [ ] **Step 1: Failing tests:** `test_migrate_fresh_returns_1`, `test_migrate_is_idempotent` (second call returns 1, no error), `test_morning_defaults_seeded` (row has `wake_time=='07:45'`, `count==3`), `test_rating_requires_some_field` (insert with all three NULL raises `sqlite3.IntegrityError`), `test_satisfaction_out_of_range_rejected` (11 raises).
- [ ] **Step 2:** `pytest tests/db -v`. Expect FAIL.
- [ ] **Step 3:** Implement `connect` and `migrate`. Add the `db` fixture to `tests/conftest.py`.
- [ ] **Step 4:** `pytest tests/db -v`. Expect PASS.
- [ ] **Step 5:** Commit `feat: sqlite schema and migrations`.

### Task 3: Store: videos, plays, ratings, history

**Files:** Create `src/store/__init__.py`, `src/store/videos.py`; Test `tests/store/test_videos.py`

**Interfaces:**
- Produces (all take `conn` first):
  - `@dataclass(frozen=True) Video(video_id, title, channel_name, channel_id, description, duration_s, published_at, tags: tuple[str,...], category, chapters: tuple[Chapter,...], thumbnail_url, view_count, like_count, transcript: str|None)`, `Chapter(title: str, start_s: float)`
  - `upsert_video(conn, v: Video) -> None` (keeps the existing `transcript` and `first_seen` when the new value is None); `get_video(conn, video_id) -> Video | None`; `set_transcript(conn, video_id, text) -> None`
  - `start_play(conn, video_id, queue_item_id: int|None) -> int`; `end_play(conn, play_id, seconds: float, reason: str) -> None`
  - `add_rating(conn, video_id, liked: bool|None, satisfaction: int|None, notes: str|None) -> int` (raises `ValueError` if all None or satisfaction outside 1–10)
  - `HistoryEntry(play_id, video: Video, started_at, seconds_listened, end_reason, source: str|None, latest_rating: Rating|None)`, `Rating(id, liked, satisfaction, notes, created_at)`
  - `history(conn, *, unrated_only=False, limit=50, offset=0) -> list[HistoryEntry]` newest first. `source` joins from `queue_items`.
  - `recent_feedback(conn, limit=50) -> list[HistoryEntry]` (rated or skipped only, for agent context)
  - `played_video_ids(conn, since_days: int) -> set[str]`; `disliked_video_ids(conn) -> set[str]`

- [ ] **Step 1: Failing tests:** `test_upsert_preserves_transcript`, `test_play_lifecycle` (start then end skipped at 192.0; history shows `end_reason=="skipped"`, `seconds_listened==192.0`), `test_multiple_ratings_kept_latest_shown` (two ratings, `SELECT COUNT(*)`==2, history `latest_rating.satisfaction` is the second), `test_add_rating_notes_only_ok`, `test_add_rating_all_none_raises`, `test_unrated_filter`, `test_disliked_ids` (liked=0 counted).
- [ ] **Step 2:** `pytest tests/store/test_videos.py -v`. Expect FAIL.
- [ ] **Step 3:** Implement with parameterized SQL only. Tags and chapters are stored as JSON.
- [ ] **Step 4:** Run it. Expect PASS.
- [ ] **Step 5:** Commit `feat: video, play and rating store`.

### Task 4: Store: queue, agent requests, taste, settings, quota, status

**Files:** Create `src/store/queue.py`, `src/store/agent.py`, `src/store/settings.py`; Test `tests/store/test_queue.py`, `tests/store/test_agent_store.py`, `tests/store/test_settings.py`

**Interfaces:**
- `queue.py`: `QueueItem(id, video: Video, position, source, reason, agent_request_id, status, added_at)`; `enqueue(conn, video_id, source, reason=None, agent_request_id=None, where: Literal["next","end"]="end") -> int` ("next" puts it right after the `playing` item, using fractional positions); `upcoming(conn) -> list[QueueItem]` (status `queued`, by position); `current(conn) -> QueueItem|None` (`playing`); `mark(conn, item_id, status)`; `pop_next(conn) -> QueueItem|None` (marks the current item `played`, the next `queued` one `playing`, returns it); `previous(conn) -> QueueItem|None` (latest `played` item becomes `playing` again, the current one goes back to `queued` at the front); `remove(conn, item_id)`; `reorder(conn, ids: list[int])` (raises `ValueError` unless ids equal the set of queued ids); `clear(conn)`; `remaining_seconds(conn) -> int`.
- `agent.py`: `AgentRequest(id, prompt, kind, play, status, summary, error, created_at, finished_at)`; `create_request(conn, prompt, kind, play) -> int`; `set_status(conn, id, status, *, summary=None, error=None)`; `get_request`, `list_requests(conn, limit=20)`, `queued_for_request(conn, id) -> list[QueueItem]`; `TasteProfile(id, summary, likes: tuple[str,...], dislikes: tuple[str,...], source, created_at)`; `save_taste(conn, summary, likes, dislikes, source) -> int`; `latest_taste(conn) -> TasteProfile|None`; `add_correction(conn, text) -> int`; `pending_corrections(conn) -> list[tuple[int,str]]`; `mark_corrections_applied(conn, ids)`; `ratings_since_last_taste(conn) -> int`.
- `settings.py`: `MorningSettings(enabled: bool, wake_time: str, days: tuple[int,...], topics: tuple[str,...], count: int)`; `get_morning`, `save_morning(conn, s)` (validates `HH:MM`, days in 0–6, 1 ≤ count ≤ 6, topics non-empty, else `ValueError`); `add_quota(conn, day: str, units: int) -> int` (new total); `quota_used(conn, day) -> int`; `log_event(conn, component, level, message)`; `recent_events(conn, limit=20)`; `latest_event(conn, component) -> tuple[level, message, created_at] | None`; `record_import(conn, kind, *, video_id=None, channel_id=None, title=None)`; `imported(conn, kind) -> list[Row]`.

- [ ] **Step 1: Failing tests:** `test_enqueue_next_goes_after_playing`, `test_pop_next_advances_and_marks_played`, `test_pop_next_empty_returns_none`, `test_previous_restores_last_played`, `test_reorder_rejects_mismatched_ids`, `test_remaining_seconds_sums_queued`, `test_taste_versions_latest_wins`, `test_request_status_transitions`, `test_save_morning_rejects_bad_time` ("7:45", "25:00"), `test_save_morning_roundtrip`, `test_quota_accumulates_per_day`.
- [ ] **Step 2:** Run `pytest tests/store -v`. Expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run it. Expect PASS.
- [ ] **Step 5:** Commit `feat: queue, agent, taste and settings stores`.

### Task 5: Playback engine

**Files:** Create `src/player/__init__.py`, `src/player/mpv.py`, `src/player/engine.py`; Test `tests/player/test_engine.py`

**Interfaces:**
- `mpv.py`: `class Mpv(Protocol)`: `load(url: str) -> None`, `stop()`, `set_pause(paused: bool)`, `seek_absolute(seconds: float)`, `position() -> float|None`, `duration() -> float|None`, `paused() -> bool`, `wait_event(timeout: float) -> dict|None` (returns mpv events such as `{"event":"end-file","reason":"eof"}`). `class MpvIpc(Mpv)`: launches `mpv --idle=yes --no-video --no-terminal --input-ipc-server=<sock> --ytdl-format=bestaudio --ytdl-raw-options=cookies=<cookies_path>` and talks newline-delimited JSON over the Unix socket. No unit test (Unix sockets): it's verified on Pi3 in Task 15.
- `engine.py`: `NowPlaying(state: Literal["playing","paused","idle"], item: QueueItem|None, position_s: float, duration_s: float|None)`. `class PlaybackEngine(db_factory: Callable[[], Connection], mpv: Mpv)` with `play_next() -> QueueItem|None`, `toggle_pause()`, `seek(position_s)` (clamped to `[0, duration-1]`), `skip(delta_s)` (clamped the same way), `next()` (ends the current play as `skipped`), `previous()` (restarts the current item if position > 5 s, else `store.queue.previous`), `stop()`, `now() -> NowPlaying`, `handle_event(evt)` (on `end-file` with `reason=="eof"`: end the play `finished` and `play_next()`; `error`: end it `error`, log a status event, then `play_next()`), `run_forever(stop_event)` (loop over `wait_event(1.0)`). Uses `start_play`/`end_play`. Plays `https://www.youtube.com/watch?v=<id>`. When `pop_next` returns None: `mpv.stop()`, state `idle`.

Tests use `FakeMpv` in `tests/player/fakes.py` (records calls, scriptable position/duration, events pushed by the test).

- [ ] **Step 1: Failing tests:** `test_play_next_loads_url_and_logs_play`, `test_eof_advances_and_marks_finished`, `test_next_marks_skip_with_position` (position 192.0 → play `skipped`, `seconds_listened==192.0`), `test_queue_empty_stops_and_idle`, `test_seek_is_clamped` (−10 → 0; 9999 with duration 600 → 599), `test_previous_restarts_when_past_5s`, `test_error_event_logs_and_advances`.
- [ ] **Step 2:** `pytest tests/player -v`. Expect FAIL.
- [ ] **Step 3:** Implement. Guard engine state with a `threading.Lock`. Every public method opens its own connection via `db_factory`.
- [ ] **Step 4:** Run it. Expect PASS.
- [ ] **Step 5:** Commit `feat: mpv playback engine with seek and play logging`.

### Task 6: YouTube Data API source

**Files:** Create `src/sources/__init__.py`, `src/sources/data_api.py`; Test `tests/sources/test_data_api.py`

**Interfaces:**
- `class QuotaExceeded(Exception)`. `class DataApi(api_key: str, db_factory, ceiling: int = 9500, service=None)`. `service` is injectable for tests; the default is `googleapiclient.discovery.build("youtube","v3",developerKey=api_key, cache_discovery=False)`.
  - `search(query: str, max_results: int = 25) -> list[str]` (`type="video"`, records 100 units *before* calling, raises `QuotaExceeded` if `used + 100 > ceiling`)
  - `video_details(ids: list[str]) -> list[Video]` (batches of 50 at 1 unit each; `part="snippet,contentDetails,statistics"`; duration via `src.fetcher._parse_duration`; chapters empty here)
  - `channel_uploads(channel_id: str, max_results: int = 25) -> list[str]` (2 units: `channels.list` → uploads playlist → `playlistItems.list`)
  - The quota day is the **Pacific** date (`America/Los_Angeles`), when Google resets it.

- [ ] **Step 1: Failing tests** with a fake service: `test_search_records_100_units`, `test_search_raises_when_over_ceiling` (pre-set used 9450), `test_video_details_batches_by_50` (120 ids → 3 calls, 3 units), `test_details_maps_duration_and_counts`.
- [ ] **Step 2:** Run it. Expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run it. Expect PASS.
- [ ] **Step 5:** Commit `feat: youtube data api source with quota tracking`.

### Task 7: yt-dlp source and catalog cache

**Files:** Create `src/sources/ytdlp.py`, `src/sources/catalog.py`; Test `tests/sources/test_ytdlp.py`, `tests/sources/test_catalog.py`

**Interfaces:**
- `class SourceAuthError(Exception)` (cookies missing/expired, or the "Sign in to confirm you're not a bot" check). `class SourceError(Exception)`.
- `class YtDlp(cookies_path: str, min_interval_s: float = 2.0, ydl_factory=None, sleep=time.sleep, clock=time.monotonic)`:
  - `account_feed(kind: Literal["liked","history","recommended","subscriptions","watch_later"], limit=200) -> list[FeedItem]` mapping to `":ytfav"`, `":ythistory"`, `":ytrec"`, `":ytsubs"`, `":ytwatchlater"`; `FeedItem(video_id, title, channel_id, channel_name)`; uses `extract_flat="in_playlist"`, `playlistend=limit`
  - `subscribed_channels() -> list[FeedItem]` from `https://www.youtube.com/feed/channels` (video_id empty)
  - `metadata(video_id) -> Video` (includes `chapters` from yt-dlp `chapters`, `tags`, `categories[0]`)
  - `transcript(video_id, lang="en") -> str | None`: prefer manual subtitles, then automatic captions in `json3` format, fetched through the same YoutubeDL session; the text is joined and whitespace-collapsed; returns None if there are none
  - `search(query, n=20) -> list[str]` via `f"ytsearch{n}:{query}"`, flat
  - Paces every call to ≥ `min_interval_s` apart. Maps `yt_dlp.utils.DownloadError` whose message contains `"Sign in to confirm"`, `"cookies"` or `"login"` to `SourceAuthError`, and other errors to `SourceError`. Never passes `mark_watched`.
- `catalog.py`: `ensure_videos(conn, ids, data_api, ytdlp) -> list[Video]` (fetches only ids not already in `videos`, via `data_api.video_details`; on `QuotaExceeded`, falls back to `ytdlp.metadata` one at a time); `ensure_transcript(conn, video_id, ytdlp, max_chars=3000) -> str|None` (cached in `videos.transcript`, truncated with `src.transcript.truncate_transcript`); `ensure_chapters(conn, video_id, ytdlp)` (fills empty chapters from `ytdlp.metadata`, called by the engine before playing).

- [ ] **Step 1: Failing tests** with a fake `YoutubeDL` factory: `test_feed_kind_maps_to_ytdlp_url`, `test_calls_are_paced` (fake clock/sleep, two calls → sleep(2.0)), `test_bot_check_raises_source_auth_error`, `test_never_sets_mark_watched` (inspect the opts dict), `test_transcript_prefers_manual`, `test_catalog_skips_known_ids` (known id → details called only with unknown), `test_catalog_falls_back_on_quota`, `test_transcript_cached_second_call_no_fetch`.
- [ ] **Step 2:** Run it. Expect FAIL.
- [ ] **Step 3:** Implement. Call `ensure_chapters` from `PlaybackEngine.play_next` via an optional `on_before_play` callback (add a one-line hook to Task 5's engine and a test, `test_on_before_play_called`).
- [ ] **Step 4:** Run it. Expect PASS.
- [ ] **Step 5:** Commit `feat: yt-dlp source and catalog cache`.

### Task 8: Claude CLI wrapper

**Files:** Create `src/agent/__init__.py`, `src/agent/claude_cli.py`; Test `tests/agent/test_claude_cli.py`

**Interfaces:**
- Errors: `ClaudeError(Exception)` with subclasses `ClaudeNotFound`, `ClaudeNotLoggedIn`, `ClaudeRateLimited`, `ClaudeTimeout`, `ClaudeBadOutput`.
- `run_claude(prompt: str, *, command="claude", timeout_s=300, runner=subprocess.run) -> str` runs `[command, "-p", prompt]` with text output. Non-zero exit or output containing `"usage limit"`/`"rate limit"` (case-insensitive) → `ClaudeRateLimited`; `"/login"`/`"not logged in"`/`"invalid api key"` → `ClaudeNotLoggedIn`; `FileNotFoundError` → `ClaudeNotFound`; `TimeoutExpired` → `ClaudeTimeout`.
- `extract_json(text: str) -> dict | list` (moved from `src/analyzer._extract_json`: fenced block, else the first balanced `{...}`/`[...]`; raises `ClaudeBadOutput`).
- `class ClaudeRunner(Protocol): def __call__(self, prompt: str) -> str`. Later tasks receive a runner. Tests pass `FakeClaude` (`tests/agent/fakes.py`), which returns scripted responses in order and records prompts.

- [ ] **Step 1: Failing tests:** one per error mapping, plus `test_extract_json_fenced`, `test_extract_json_bare_object`, `test_extract_json_garbage_raises`.
- [ ] **Step 2:** Run. Expect FAIL. **Step 3:** Implement. **Step 4:** Run. Expect PASS.
- [ ] **Step 5:** Commit `feat: claude cli wrapper with error mapping`.

### Task 9: Agent request pipeline

**Files:** Create `src/agent/request.py`, `src/agent/context.py`; Test `tests/agent/test_request.py`

**Interfaces:**
- Consumes: stores (Tasks 3–4), `ensure_videos`, `ensure_transcript`, `DataApi.search`, `YtDlp.search`, `ClaudeRunner`.
- `build_taste_context(conn) -> str`: latest taste summary + likes/dislikes, then up to 50 recent feedback lines formatted `"<title> | <channel> | liked=<y/n/-> sat=<n|-> | <notes or 'skipped at m:ss'>"`.
- `run_request(conn, request_id: int, *, claude: ClaudeRunner, data_api, ytdlp, source: Literal["agent","morning"]="agent") -> list[int]` (queue item ids). Three Claude calls, each asking for JSON only:
  1. **Plan**: request text + taste context → `{"queries": [≤4 strings], "count": int (1–12), "target_minutes": int|null}`.
  2. **Shortlist**: for each query, `data_api.search(q, 25)`, falling back to `ytdlp.search(q, 20)` on `QuotaExceeded`. Dedupe, drop `played_video_ids(conn, 30)` and `disliked_video_ids`, then `ensure_videos`. Cap at 40, sent as metadata lines → `{"shortlist": [≤8 video_ids]}`.
  3. **Pick**: shortlist with transcripts (`ensure_transcript`, 3000 chars) + taste context → `{"picks": [{"video_id": str, "reason": str ≤140 chars}]}` in play order, length ≤ `count`.
  - Ignores picked ids that weren't in the shortlist. Enqueues picks (`where="next"` if the request's `play=="now"`, else `"end"`) with `agent_request_id`. Sets the request `done` with summary `"Queued N videos (M min)"`. On any `ClaudeError`/`SourceError`/empty picks: sets `failed` with `error=str(exc)` or `"no suitable videos found"`, logs a status event (`component="agent"`), enqueues nothing, and re-raises nothing.
  - Returns the inserted queue ids.

- [ ] **Step 1: Failing tests** with FakeClaude, fake DataApi, fake YtDlp: `test_happy_path_enqueues_picks_with_reasons`, `test_play_now_inserts_next`, `test_falls_back_to_ytdlp_search_on_quota`, `test_excludes_disliked_and_recently_played`, `test_unknown_pick_ids_ignored`, `test_rate_limited_marks_failed` (FakeClaude raises `ClaudeRateLimited` → status `failed`, error contains "rate"), `test_prompt_contains_request_and_taste` (prompt 1 contains the request text and the latest taste summary).
- [ ] **Step 2:** Run. Expect FAIL. **Step 3:** Implement. **Step 4:** Run. Expect PASS.
- [ ] **Step 5:** Commit `feat: agent request pipeline`.

### Task 10: Taste profile

**Files:** Create `src/agent/taste.py`; Test `tests/agent/test_taste.py`

**Interfaces:**
- `refresh_taste(conn, *, claude) -> TasteProfile`: prompt = current taste + `build_taste_context` + pending corrections (quoted as the user's own words, which take priority) → `{"summary": str (≤400 chars), "likes": [≤10], "dislikes": [≤10]}`. Saves with `source="agent"` and marks corrections applied.
- `should_refresh(conn) -> bool`: True when `ratings_since_last_taste(conn) >= 5`, or there are pending corrections, or no taste exists and at least 1 rating does.
- `remove_trait(conn, kind: Literal["likes","dislikes"], text: str) -> TasteProfile`: copies the latest profile without that item, `source="user"`. `ValueError` if not present.
- `seed_taste_from_imports(conn, *, claude) -> TasteProfile | None`: when no taste exists, builds one from `account_imports` titles/channels (≤200 lines). Used by `import-account` (Task 14).

- [ ] **Step 1: Failing tests:** `test_refresh_saves_new_version`, `test_corrections_in_prompt_and_marked_applied`, `test_should_refresh_after_5_ratings`, `test_remove_trait_creates_user_version`, `test_remove_missing_trait_raises`.
- [ ] **Step 2:** Run. Expect FAIL. **Step 3:** Implement. **Step 4:** Run. Expect PASS.
- [ ] **Step 5:** Commit `feat: taste profile refresh and corrections`.

### Task 11: Morning set and scheduler

**Files:** Create `src/agent/morning.py`, `src/scheduler.py`; Test `tests/agent/test_morning.py`, `tests/test_scheduler.py`

**Interfaces:**
- `morning_prompt(settings: MorningSettings) -> str`: builds the request text. It must contain every topic and the count, ask for podcast-style or long-form videos, and contain no channel or creator names (the test asserts the prompt is built only from the topics plus fixed template text).
- `fallback_morning(conn, count: int) -> list[int]`: when the agent fails, enqueues up to `count` videos with `source="fallback"`, ordered by: (a) videos from `account_imports` kinds `recommended`/`subscription` that are in `videos`, never played, never disliked, longest first; then (b) the highest-satisfaction rated videos not played in 14 days. Returns queue ids. Logs a status event either way.
- `class MorningScheduler(db_factory, *, run_request_fn, engine, tz="America/Chicago", clock=datetime.now)`, method `tick(now: datetime) -> list[str]` (returns actions taken, for tests), called every 20 s by a thread.
  - At `wake_time − 5 min` on an enabled day, once per local date: create an `agent_requests` row (`kind="morning"`, `play="end"`), first clearing queued items with `source in ('morning','fallback')` left from earlier days, then call `run_request_fn` synchronously. If it fails or returns no items, call `fallback_morning`. Action `"built"`/`"fallback"`.
  - At `wake_time`, once per date: if the engine is idle and morning items are queued, move them to the front (`reorder`) and `engine.play_next()`. If something else is already playing, leave it alone. Action `"started"`.
  - A missed window (the Pi was off) is skipped, never run late. Guarded by a `last_built`/`last_started` local date stored in `status_events`.
- `run_morning_now(...)`: the "Test it now" button: build immediately and start.

- [ ] **Step 1: Failing tests:** `test_prompt_has_topics_and_no_names` (settings topics `["technology"]` → prompt contains "technology"; assert the prompt equals the template rendered with only those inputs), `test_build_at_0740_central` (now = 07:40 America/Chicago → "built"), `test_no_build_on_disabled_day`, `test_build_once_per_day` (two ticks at 07:40:10 and 07:40:30 → one build), `test_start_at_0745_when_idle`, `test_no_start_when_already_playing`, `test_fallback_used_when_agent_fails`, `test_dst_safe` (2026-03-08 and 2026-11-01 dates still fire at local 07:40).
- [ ] **Step 2:** Run. Expect FAIL. **Step 3:** Implement with `zoneinfo.ZoneInfo`. **Step 4:** Run. Expect PASS.
- [ ] **Step 5:** Commit `feat: morning set builder, fallback and scheduler`.

### Task 12: Agent worker, Bluetooth and legacy migration

**Files:** Create `src/agent/worker.py`, `src/bluetooth.py`, `src/legacy.py`; Test `tests/agent/test_worker.py`, `tests/test_bluetooth.py`, `tests/test_legacy.py`

**Interfaces:**
- `class AgentWorker(db_factory, run_request_fn, engine, refresh_taste_fn)`: `submit(request_id: int)` puts the id on a `queue.Queue`; a single daemon thread runs requests one at a time (status `running` → done/failed). After a `play=="now"` request finishes with items while the engine is idle, it calls `engine.play_next()`. After each request, if `should_refresh`, it calls `refresh_taste_fn`. On startup, any `running` rows are reset to `failed` with error `"server restarted"`.
- `bluetooth.py`: `is_connected(mac: str, run=subprocess.run) -> bool` (`bluetoothctl info <mac>` contains `Connected: yes`); `ensure_connected(mac, adapter="hci1", run=...) -> bool` (`bluetoothctl select <adapter controller address>` then `connect <mac>`; the controller address comes from `bluetoothctl list` matching the `hci1` line via `hciconfig hci1`); `class BtMonitor(mac, adapter, interval_s=30)` thread that keeps `last_state: Literal["connected","disconnected","unconfigured"]` (`unconfigured` when mac is empty) and retries `ensure_connected` when disconnected.
- `legacy.py`: `migrate_legacy(conn, data_dir: Path) -> dict[str,int]`. Reads `feedback.json` (entries `{video_id,title,rating,comment,timestamp}`; `thumbs_up`→liked 1, `thumbs_down`→0, `neutral`→None; `comment`→notes; skipped if it would violate the rating check). Upserts a minimal `Video(title=…)` first. Reads `interest_profile.json` → `save_taste(..., source="legacy")`, where `summary` is a one-line join of its top topics and `likes` are those topics (weight > 0) and `dislikes` (weight < 0). Idempotent: skips if a `legacy` taste already exists. Returns counts.

- [ ] **Step 1: Failing tests:** `test_worker_runs_in_order_and_starts_idle_engine`, `test_worker_resets_running_on_start`, `test_is_connected_parses_output`, `test_monitor_unconfigured_when_no_mac`, `test_status_reports_disconnected` (fake run returns `Connected: no` → `last_state=="disconnected"` and retry called), `test_legacy_maps_ratings`, `test_legacy_is_idempotent`.
- [ ] **Step 2:** Run. Expect FAIL. **Step 3:** Implement. **Step 4:** Run. Expect PASS.
- [ ] **Step 5:** Commit `feat: agent worker, bluetooth monitor, legacy import`.

### Task 13: REST API and contract doc

**Files:** Create `src/api/__init__.py`, `src/api/app.py`, `src/api/routes_player.py`, `routes_queue.py`, `routes_library.py`, `routes_agent.py`, `routes_system.py`, `docs/api.md`; Delete `src/api.py`, `tests/test_api.py`; Test `tests/api/test_*.py` (FastAPI `TestClient`)

**Interfaces:**
- `@dataclass Deps(db_factory, engine, worker, data_api, ytdlp, bt: BtMonitor, scheduler_run_now, config)`; `create_app(deps: Deps) -> FastAPI`. Envelope helpers `ok(data, status=200)` and `fail(message, status)`. Validation errors also come back enveloped (custom `RequestValidationError` handler → 422). Every route lives under `/api`.
- Endpoints (exact paths and payloads, copied into `docs/api.md` with example JSON):

| Method & path | Body | `data` |
|---|---|---|
| GET `/api/now` | – | `{state, item: QueueItemOut\|null, position_s, duration_s, output_device}` |
| POST `/api/player/toggle` · `/next` · `/previous` · `/stop` | – | NowPlaying |
| POST `/api/player/seek` | `{position_s: float}` | NowPlaying |
| POST `/api/player/skip` | `{delta_s: float}` (e.g. −15, 30) | NowPlaying |
| GET `/api/queue` | – | `{items: [QueueItemOut], remaining_s}` |
| POST `/api/queue` | `{video_id?: str, url?: str, where: "next"\|"end"}` | QueueItemOut (201) |
| DELETE `/api/queue/{id}` · DELETE `/api/queue` | – | `{removed: int}` |
| PUT `/api/queue/order` | `{ids: [int]}` | `{items}` |
| GET `/api/search?q=` | – | `{results: [VideoOut]}` (Data API, yt-dlp fallback) |
| POST `/api/ratings` | `{video_id, liked?: bool, satisfaction?: 1–10, notes?: str ≤2000}` | `{id}` (201) |
| GET `/api/history?unrated=&limit=&offset=` | – | `{entries: [HistoryOut]}` |
| GET `/api/taste` | – | `{summary, likes, dislikes, updated_at}` or null |
| POST `/api/taste/corrections` | `{text: str ≤1000}` | `{id}` (202; triggers a refresh via the worker) |
| DELETE `/api/taste/traits` | `{kind: "likes"\|"dislikes", text}` | taste |
| POST `/api/agent/requests` | `{prompt: str 1–1000, play: "now"\|"end"}` | `{id}` (202) |
| GET `/api/agent/requests/{id}` · GET `/api/agent/requests?limit=` | – | `{id, prompt, status, summary, error, items: [QueueItemOut], created_at, finished_at}` |
| GET `/api/morning` · PUT `/api/morning` | `{enabled, wake_time "HH:MM", days [0–6], topics [str], count 1–6}` | settings |
| POST `/api/morning/test` | – | `{request_id}` (202) |
| GET `/api/status` | – | `{player: state, bluetooth: "connected"\|"disconnected"\|"unconfigured", cookies: "ok"\|"expired"\|"missing", claude: "ok"\|"rate_limited"\|"not_logged_in"\|"missing"\|"unknown", ytdlp_version, quota_used_today, quota_ceiling, events: [{component, level, message, created_at}]}` |

  `QueueItemOut = {id, video_id, title, channel, thumbnail_url, duration_s, source, reason, chapters: [{title, start_s}]}`; `VideoOut` = the video fields of that; `HistoryOut = {play_id, video: VideoOut, started_at, seconds_listened, end_reason, source, rating: {liked, satisfaction, notes, created_at}|null}`.
  `cookies` status: `missing` if the file doesn't exist; `expired` if the latest `ytdlp` status event is a `SourceAuthError`; else `ok`. `claude` status comes from the latest `agent` status event's error class name.
  `url` in POST `/api/queue` accepts `youtube.com/watch?v=`, `youtu.be/` and `youtube.com/shorts/`; anything else gives 400 `"not a YouTube video link"`.

- [ ] **Step 1: Failing tests** with fake deps: one happy-path test per row above, plus `test_envelope_on_validation_error` (satisfaction 11 → 422, `ok:false`, error mentions satisfaction), `test_queue_post_rejects_non_youtube_url`, `test_agent_request_202_and_worker_submit_called`, `test_status_cookies_missing`, `test_status_cookies_expired_after_auth_error`, `test_seek_endpoint_clamps` (via the engine fake).
- [ ] **Step 2:** `pytest tests/api -v`. Expect FAIL. **Step 3:** Implement the routes and write `docs/api.md`. **Step 4:** Run. Expect PASS.
- [ ] **Step 5:** Commit `feat: home studio rest api and contract doc`.

### Task 14: Entry point, account import and cleanup

**Files:** Rewrite `src/__main__.py`; Delete `src/player.py`, `src/analyzer.py`, `src/profile.py`, `src/playlist.py`, `tests/test_analyzer.py`, `tests/test_profile.py`, `scripts/setup-snapcast-client.sh`, `scripts/setup-snapcast-server.sh`, `scripts/deploy-pi2.sh`, `scripts/install-cron.sh`, `scripts/setup-systemd-api.sh`; Test `tests/test_main.py`

**Interfaces:**
- `python -m src serve`: `load_config`, `connect`+`migrate`, builds `MpvIpc`, `PlaybackEngine` (thread), `DataApi`, `YtDlp`, `AgentWorker`, `BtMonitor`, `MorningScheduler` (20 s thread), `create_app`, then `uvicorn.run(host, port)`. The YouTube API key is optional: without it, search goes straight to yt-dlp.
- `python -m src import-account`: pulls `subscribed_channels` + `account_feed` for `liked`, `history`, `recommended`, `subscriptions`, `watch_later` (limit 200 each) into `account_imports`, then `ensure_videos` for up to 300 video ids (paced), then `seed_taste_from_imports`. Prints counts. Also run daily at 04:00 local by the scheduler (add `tick` action `"imported"`).
- `python -m src migrate-legacy [--data-dir data]`: calls `migrate_legacy`.
- `build_services(config) -> Deps` is its own function so `tests/test_main.py` can assert wiring with fakes.

- [ ] **Step 1: Failing tests:** `test_cli_parses_commands`, `test_build_services_without_api_key_uses_ytdlp_search`, `test_import_account_records_and_seeds` (fakes).
- [ ] **Step 2:** Run. Expect FAIL. **Step 3:** Implement and delete the obsolete files. **Step 4:** Run the **whole suite**: `pytest -q`. Expect all PASS and no import of deleted modules (`grep -r "src.analyzer\|src.profile\|src.player import\|snapcast" src tests` returns nothing).
- [ ] **Step 5:** Commit `refactor: new entry point, account import, remove snapcast pipeline`.

### Task 15: Pi3 deployment and on-device verification

**Files:** Create `systemd/home-studio.service`, `systemd/ytdlp-update.service`, `systemd/ytdlp-update.timer`, `scripts/setup-pi3.sh`, `scripts/deploy-pi3.sh`; Modify `scripts/pair-bluetooth.sh`, `README.md`

**Requirements:**
- `setup-pi3.sh` (run once on the Pi): `apt install mpv bluez pipewire pipewire-pulse wireplumber libspa-0.2-bluetooth python3-venv`, then creates `~/home-studio/.venv` and `~/home-studio/secrets` (mode 700). It prints the next manual steps: put `cookies.txt` in `secrets/`, write `config.local.yaml`, and `claude` login (install Claude Code CLI with `npm i -g @anthropic-ai/claude-code`, then run `claude` once to log in).
- `deploy-pi3.sh`: host from env `HOME_STUDIO_HOST` (never committed), runs over WSL `rsync -az --exclude data --exclude secrets --exclude config.local.yaml --exclude .git`, then `pip install -e .`, then `python -m src migrate-legacy` on first deploy only, then restarts the service.
- `home-studio.service`: runs as user `jamil` so the PipeWire user session and the `claude` login are available. Needs `loginctl enable-linger jamil` in the setup script. `WorkingDirectory=/home/jamil/home-studio`, `ExecStart=.venv/bin/python -m src serve`, `Restart=on-failure`, `Nice=5`, `MemoryMax=512M`. Installed as a **user** unit (`~/.config/systemd/user/`).
- `ytdlp-update.timer`: daily at 03:30 runs `.venv/bin/pip install -U yt-dlp`.
- `pair-bluetooth.sh`: takes `<MAC> [adapter=hci1]`, runs `select` on the hci1 controller before `pair/trust/connect`, and sets the Echo as the default PipeWire sink.
- README: plain-language setup for Jamil: pairing ("Alexa, pair Bluetooth"), cookies export, deploy, and checking `/api/status`.

- [ ] **Step 1:** Run `setup-pi3.sh` on Pi3 (SSH via WSL). Pair the Echo with `pair-bluetooth.sh <echo-mac>`. Expect `bluetoothctl info <mac>` to show `Connected: yes`.
- [ ] **Step 2:** Deploy. `curl -s http://localhost:8765/api/status` on the Pi. Expect `ok:true`, `bluetooth:"connected"`, `cookies:"ok"`.
- [ ] **Step 3:** `curl -X POST /api/queue -d '{"url":"<any public video link>","where":"end"}'` and then `POST /api/player/next`. Expect audio from the Echo. Then `POST /api/player/seek {"position_s":120}`. Expect `GET /api/now` to show `position_s` ≈ 120 and the audio to jump.
- [ ] **Step 4:** `POST /api/agent/requests {"prompt":"a short beginner ROS 2 video","play":"now"}`. Poll `GET /api/agent/requests/{id}` until `done`. Expect it to be queued and playing.
- [ ] **Step 5:** `POST /api/morning/test`. Expect a request with `kind morning`, items queued, and playback starting.
- [ ] **Step 6:** Check memory: `systemctl --user status home-studio` under 512M, and `systemctl status ntopng` still active.
- [ ] **Step 7:** Commit `feat: pi3 deployment, systemd units, bluetooth pairing`. Push the branch and open a PR to `master`.

---

## Self-review notes
- Spec coverage: hardware/hci1 (T12, T15), SQLite model (T2–4), mpv seek/chapters/skip (T5, T7), hybrid sources and quota (T6–7), no mark-watched (T7 test), cookies-expired status (T7, T13), Claude CLI (T8), Agent tab (T9, T13), morning set at 7:40/7:45 CT with fallback and no creator names (T11), queue stops when empty (T5), taste and corrections (T10), legacy migration (T12), API for every approved screen (T13), public-repo secrets (Global, T1, T15), ntopng limits (T15), yt-dlp nightly update (T15). Account import / day-one seeding (T14).
- Deviation from spec: the morning trigger is an in-process scheduler instead of a systemd timer, because the time is editable from the app at runtime. The spec marked this as builder's call.
- Deferred from spec builder's calls: automatic SponsorBlock skipping (add later as an mpv `sponsorblock` script or yt-dlp `--sponsorblock-remove` once playback is stable); Pi-hosted APK updates.
- Not in this plan (spec §9 "Later"): OAuth write-back, browsing UI, synced video, packaging, voice.
