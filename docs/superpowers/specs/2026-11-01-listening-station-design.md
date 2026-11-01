# Personal Listening Station: Design

**Date:** 2026-11-01
**Status:** Draft, source of truth for this work
**Product name:** TBD (rebrand of YoutubeBrainwash + AlarmAPI)

This document comes out of a design conversation with Jamil and **supersedes
the earlier YoutubeBrainwash / AlarmAPI write-up** wherever they disagree.
Items marked *(builder's call)* were proposed by Claude and not explicitly
decided by Jamil; they can be changed.

---

## 1. Intent

A personal "YouTube radio station" for **background listening**: podcasts,
video essays, music, commentary. A Raspberry Pi plays YouTube audio through
an Amazon Echo used as a plain Bluetooth speaker. Jamil rates what they hear
from their phone. An AI agent reads that feedback and keeps finding videos they'll
like, so the system becomes **their own local YouTube algorithm**, steered by
their ratings instead of YouTube's.

### Why
YouTube's recommendations aren't theirs. Jamil wants a recommender whose only
input is what they say they liked and didn't, and which explains its picks.

### Success looks like
- Audio plays on the Echo from the Pi with no screen involved.
- Rating something takes seconds from the phone.
- Over weeks, AI picks are noticeably better than YouTube's.
- The system is easy to extend (new feedback fields, new sources, new features).

### Non-goals (for now)
- Watching video synced on the phone (parked; see §9).
- Voice control / voice rating via the Echo.
- Packaging for friends & family (future; don't block it, don't build for it).
- Writing back to YouTube (likes, playlists): planned later via official API.
- Multi-room / multi-speaker audio.

### Anti-goals (would be a failure even if it "works")
- Rating feels like a chore → Jamil stops doing it → the algorithm starves.
- AI lookups look like a bot and get the YouTube account flagged.
- It breaks silently when YouTube changes something.
- Adding a feature means reworking the database or core.

---

## 2. System overview

Two repos, renamed from existing projects:

| Role | Existing repo | Becomes |
|---|---|---|
| **Pi server**: database, player, AI agent, API | `JamilCPU/YoutubeBrainwash` | `<name>-server` |
| **Control hub**: Android app on Jamil's Pixel | `JamilCPU/AlarmAPI` | `<name>-hub` |

**No separate database repo.** The database is private to the Pi server; the
phone only ever talks to the server's API. Schema and migrations live in the
server repo. *(decided by Claude at Jamil's request)*

```
 Pixel (control hub app) ──Tailscale──▶ Pi server (FastAPI)
                                          ├─ SQLite DB (history, ratings, queue, taste)
                                          ├─ Player (yt-dlp → mpv) ──Bluetooth──▶ Echo
                                          ├─ AI agent (Claude API)
                                          └─ YouTube: Data API (browse) + yt-dlp (audio, account data)
```

---

## 3. Pi server (`YoutubeBrainwash` → `<name>-server`)

### Hardware / host
- Runs on **the Pi with the USB Bluetooth dongle Jamil just installed**:
  **Pi3** (confirmed over SSH 2026-11-01; address lives in the lab vault, not this public repo).
  The dongle is a Realtek Bluetooth 5.4 radio (USB `0bda:a728`) on **`hci1`**;
  `hci0` is the Pi's built-in UART Bluetooth. Audio must go out `hci1`.
  SSH from the Windows PC goes through WSL; user `jamil`.
- Pi3 also runs ntopng as the gateway traffic monitor; keep resource use
  modest and don't disturb it. Existing `deploy-pi2.sh` targets the wrong
  Pi and must be retargeted.
- The Pi is the **home for everything**: all data lives there.
- Reached by the phone at a **fixed Tailscale address**.

### Stack
- Python, FastAPI (already in the repo).
- **SQLite** replaces the current JSON files (`current_feed.json`,
  `feedback.json`, `interest_profile.json`). *(builder's call)*
- Playback: **yt-dlp → mpv**, audio only, out to the Echo over Bluetooth
  (BlueZ + PipeWire/PulseAudio). mpv is chosen because its IPC socket gives
  exact position, duration, pause, and seek, which the Now Playing screen
  needs. **This replaces the existing ffmpeg → Snapcast FIFO pipeline**, which
  can't seek. Snapcast is parked. *(builder's call)*
- `scripts/pair-bluetooth.sh` is reused for pairing the Echo
  ("Alexa, pair Bluetooth").
- Bluetooth auto-reconnect on boot/drop. *(builder's call)*

### YouTube access: hybrid (decided)
| Job | Source |
|---|---|
| Search, video details, channel details, new uploads from channels | **YouTube Data API v3**, **API key only** (no OAuth yet) |
| Audio stream for playback | **yt-dlp** (no official alternative exists) |
| Transcripts/captions (for AI understanding) | **yt-dlp**, only for shortlisted candidates |
| Subscriptions, liked videos, playlists, watch history, YouTube home recommendations | **yt-dlp with account cookies**: full pull on first run, then once daily |
| Search fallback when API quota is exhausted | yt-dlp search, slowly paced |

- Account: **Jamil's main YouTube Premium account** for yt-dlp cookies (decided;
  Jamil accepted the ToS risk after discussion).
- **Do not mark videos watched on YouTube.** The Pi's own history is the
  truth. Accepted side effect: YouTube's own recommendations for the account
  will go stale over time.
- **Anti-bot hygiene (required):** pace and cache all lookups; never re-fetch
  metadata/transcripts already stored; keep yt-dlp traffic modest.
- **Quota awareness:** free tier is 10,000 units/day. `videos.list` /
  `playlistItems.list` / `channels.list` ≈ 1 unit per call (up to 50 items);
  `search.list` = 100 units. Track daily usage in the DB.
- **yt-dlp auto-updates nightly.** When YouTube breaks it, the phone app
  shows a clear "playback/source broken" status. No silent failure.
- Later: OAuth for the official API to write likes/playlists back (§9).
  Note for then: Google Cloud OAuth apps left in "Testing" have refresh
  tokens that expire every 7 days; publish to "Production".
- SponsorBlock: skip sponsor segments automatically. *(builder's call)*

### Data model (must be extensible)
Every played item is logged even without a rating. Ratings are a **separate
table** linked to the video so new feedback types can be added without
migrations to core tables. Suggested tables *(builder's call on exact shape)*:

- **videos**: id, title, channel, duration, upload date, tags, category,
  description, chapters, thumbnail, view/like counts, transcript (nullable),
  source metadata, first_seen.
- **plays**: video, started_at, ended_at, seconds listened, how it ended
  (finished / skipped at X / stopped). Skips are a feedback signal.
- **ratings** (decided fields):
  - **like / dislike**
  - **satisfaction 1–10**
  - **personal notes** (free text)
  - timestamp; one video may be rated more than once (keep history).
- **queue**: ordered items with origin (AI pick / user added / subscription)
  and the AI's "why" text.
- **taste profile**: the AI's plain-language summary of Jamil's taste,
  editable by Jamil, versioned.
- **account imports**: subscriptions, likes, playlists, history pulled from YouTube.
- **api quota usage**, **system status/events**.

Schema changes go through migrations.

### AI agent
- **Runs through the `claude` CLI on Jamil's Claude subscription** (decided).
  No pay-per-token Claude API. The CLI must be installed and logged in on Pi3.
  Respect subscription rate limits: keep prompts lean (truncated or summarized
  transcripts, shortlists instead of every candidate) and handle a
  "rate-limited / not logged in" result with a clear app status.
- **A separate component** that only *reads* history/ratings/taste and
  *writes* queue entries (+ reasons) and taste-profile updates. It can be
  swapped or upgraded without touching player, API, or app.
- Day one: seeds from imported subscriptions, likes, history, and YouTube
  recommendations so it starts roughly matching their current YouTube taste;
  ratings take over from there.
- Every pick carries a short **"picked because…"** line shown in the app.
- Uses transcripts to understand what a video is actually about.
- Existing modules (`fetcher`, `transcript`, `analyzer`, `profile`,
  `playlist`) are starting material, not constraints.
### How the queue gets filled: two modes (decided)

**Mode 1: on request (the main way Jamil uses it).** The entry point is
Jamil opening Home Studio, going to the **Agent tab**, and typing a request in
plain language. Examples of the kind of thing Jamil will ask *(illustrative only)*:
"give me a podcast on <topic>", "make me a classical music playlist",
"recommend videos for learning ROS 2". The agent works out what fits, using
the request plus their taste/ratings, then queues **one video or a set**, which
starts playing on the Pi when it's done. Requests can take a while, so the
Agent tab shows that the agent is working and then what it queued and why.
Past requests stay visible in the tab. *(builder's call)*

**Mode 2: the morning queue (scheduled).**
- Jamil wakes around **7:45 a.m. US Central** (`America/Chicago`).
- At **7:40** the agent builds a morning set of **a couple of different
  videos**: podcast-style or long-form, about **technology, AI, hardware,
  engineering, or news/politics**.
- Playback **starts automatically at 7:45** on the Echo.
- **Do not hard-code specific channels or creators** into the morning
  prompt, defaults or config. Jamil doesn't want the agent anchored to
  particular sources. Picks come from the topic areas plus their ratings.
- Runs on the **Pi** (systemd timer), so it works even if the phone is
  asleep. *(builder's call)*
- Every day by default; time, days and topic areas are editable in the
  app's Alarm/Morning settings. *(builder's call)*
- If the agent fails or isn't done by 7:45, fall back to the
  best-rated unplayed candidates already in the DB so the morning is never
  silent, and show what happened in the app. *(builder's call)*

**When the queue runs out:** playback stops. The app's Now Playing shows
"queue empty" with a shortcut to the Agent tab. No endless autoplay.
*(builder's call)*

### API for the phone
REST over Tailscale. Covers: now-playing state (item, position, duration,
chapters, paused), controls (play/pause, next, previous/restart, seek to
time, skip ±N s), queue (view, add, remove, reorder), ratings (create, edit),
history, taste profile (view/edit), search/add, **agent requests** (submit a
prompt, see progress/result, list past requests), **morning schedule**
(view/edit time, days, topics), system status.
Live updates for position/state (polling or WebSocket, *builder's call*).
Auth: Tailscale network only to start. *(builder's call)*

---

## 4. Control hub app (`AlarmAPI` → `<name>-hub`)

### Platform (decided)
- **Real installed Android app** on Jamil's **Google Pixel**. Not a web app.
- Built on the existing AlarmAPI codebase: **Kotlin**, Jetpack Compose
  *(builder's call; AlarmAPI currently uses Material3 + Room + Ktor)*.
- Installed and updated **from the PC via wireless ADB**. No Play Store.
  Signed with one fixed release key so updates keep app data.
  GitHub release workflow already exists in the repo.
- Later: Pi hosts the latest APK; app shows "update available". *(builder's call)*
- Jamil is new to mobile dev: setup steps should be documented plainly.

### Role
The phone is **the control hub for everything**. The app is called
**Home Studio** (decided). The alarm is **one feature inside the hub**, not the
app's purpose. It becomes the **Morning** feature: the phone only edits
the schedule. The Pi runs it (see "Mode 2" in §3). The old on-phone alarm
(ringing, AlarmTrigger activity, embedded Ktor server) can be removed unless
a later need appears. *(builder's call)*

### Screens
1. **Now Playing** (approved by Jamil):
   - YouTube thumbnail (no video playback)
   - title, channel, "🤖 picked because…" line
   - **live elapsed and remaining time**
   - **drag-to-seek bar; seeking moves the Pi's playback**
   - chapter ticks on the bar; tap to jump *(builder's idea, shown in mockup)*
   - ⏪ 15 s / 30 s ⏩, ⏮ ⏯ ⏭
   - output device label (e.g. "Echo (Living Room)")
   - quick 👍 / 👎 and **Rate** button
   - "Open in YouTube at <current time>" link
2. **Rate**: sheet with like/dislike, 1–10 satisfaction, notes (typed or
   phone-keyboard dictation). *(sketched; not yet confirmed by Jamil)*
3. **Agent** (decided, main entry point): a prompt box for requests, an
   "agent working…" state, the result (what was queued and why), and past
   requests.
4. **Queue**: upcoming items tagged AI pick / you added / morning, with
   "why" text; Search/add. *(sketched; not confirmed)*
5. **History**: everything played + ratings. *(sketched; not confirmed)*
6. **Taste**: the AI's plain-language view of their taste, which they can
   correct. *(sketched; not confirmed)*
7. **Morning** (settings): wake time (default 7:45 CT), days, topic areas,
   on/off.

Plus: a **media notification** with play/pause/skip and 👍/👎 so a quick
rating doesn't need the app open. *(builder's idea)*

Mockups from the session: Now Playing v2 is the approved layout.

---

## 5. Constraints
- Single user (Jamil) for now; nothing should assume single-user so deeply
  that the friends-and-family version needs a rewrite.
- Everything private: Tailscale only, nothing exposed to the public internet.
- Secrets (YouTube API key, cookies, Claude API key) in gitignored config
  or env, never committed.
- **Both repos stay public** (decided by Jamil). So nothing personal or
  secret is ever committed: cookies, API keys, Tailscale names/IPs, the
  SQLite DB and personal config all stay gitignored or in env/config files
  on the Pi. Keep a `config.example` with placeholders.

## 6. Rebrand
Product name: **Home Studio** (decided by Jamil for the app).
- `JamilCPU/AlarmAPI` → **`home-studio`** (Android app). *(repo name: builder's call)*
- `JamilCPU/YoutubeBrainwash` → **`home-studio-server`** (Pi server). *(builder's call)*
- Android application ID: **`com.jamilcpu.homestudio`**, set before first
  install and never changed afterward. *(builder's call)*
GitHub keeps redirects from old names. Update package names, display name,
README, descriptions, and the Obsidian vault notes.

## 6a. Builder's calls from review
- **Content scope:** background listening means long-form and back-catalogue
  are in scope. Drop the old 48-hour recency and 5–60 min duration limits as
  defaults; make them per-request filters instead. Discovery should prefer
  cheap sources (subscriptions' uploads, related channels, imported
  likes/playlists) and spend `search.list` sparingly.
- **Cookies:** exported manually from a logged-in desktop browser as
  `cookies.txt` and uploaded to the Pi. The server detects expired cookies and
  the app shows a "cookies expired, re-upload" status. Manual refresh is fine.
- **Existing data:** migrate `feedback.json` and `interest_profile.json` into
  the new SQLite tables.

## 7. Decided by Jamil (summary)
Background listening · Pi with BT dongle is home for all data · Echo as
Bluetooth speaker · rate from phone, not voice · like/dislike + 1–10 +
notes · extensible design · real Android app on Pixel as control hub ·
Tailscale fixed address · Premium main account · hybrid YouTube API + yt-dlp,
API key for browsing · no mark-as-watched · Now Playing = remote control
with seek, not a video player · build on YoutubeBrainwash (server) and
AlarmAPI (hub, alarm as a feature) · this doc is the source of truth.

## 8. Open questions (Jamil's)
1. Confirm Rate, Queue, Agent, History, Taste screens (redraw pending).

## 9. Later
- Simple browsing of YouTube inside the phone app.
- Official API OAuth: write likes/playlists back.
- Synced video on the phone.
- Packaging for friends & family (per-household Pi, setup wizard,
  bring-your-own keys).
- Voice rating.
