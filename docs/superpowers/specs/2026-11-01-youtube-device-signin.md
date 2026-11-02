# YouTube account sign-in (device code): design

**Date:** 2026-11-01
**Status:** approved by Jamil (chose "device code" option #1)
**Extends:** `2026-11-01-listening-station-design.md`

## Why
Jamil's real YouTube taste (subscriptions, likes, playlists) should seed and keep feeding
Home Studio without relying on browser cookies, which rotate and expire. The Pi has no screen,
so sign-in must happen on the phone or PC. Google's **OAuth 2.0 device flow** ("TVs and
Limited Input devices") does exactly this: the Pi shows a short code, and Jamil enters it at
`google.com/device` on any device.

Cookies stay **optional**. They add the two things OAuth can't give (watch history and YouTube's
home recommendations), plus Premium audio and bot-check cover. Nothing breaks without them.

## Scope
- OAuth scope: `https://www.googleapis.com/auth/youtube.readonly` only (read-only).
- The Google Cloud OAuth client is type **TVs and Limited Input devices**. The consent screen
  publishing status is **In production**, so the refresh token doesn't expire after 7 days. The app
  stays unverified, so Jamil clicks through "Google hasn't verified this app" once. That's fine for
  personal use.
- The client ID and secret go in the Pi's `config.local.yaml`, as `youtube.oauth_client_id` and
  `youtube.oauth_client_secret`. They are set with a hidden-input helper script and never committed.
- Token storage: `secrets/youtube_oauth.json`, mode 600, holding the refresh token, the access
  token, the expiry and the granted scope. Never logged.

## Server behaviour
- `POST /api/youtube/oauth/start` → asks Google's device endpoint
  (`https://oauth2.googleapis.com/device/code`) for a code. Returns
  `{user_code, verification_url, expires_at, interval_s}`. A background thread then polls
  `https://oauth2.googleapis.com/token` (grant_type `urn:ietf:params:oauth:grant-type:device_code`)
  at the given interval. It honours `slow_down` and `authorization_pending`, and stops on
  `access_denied` and `expired_token`. Only one flow runs at a time; calling start again while
  pending returns the current code.
- `GET /api/youtube/oauth` → `{state: "unconfigured"|"disconnected"|"pending"|"connected"|"error",
  user_code?, verification_url?, expires_at?, error?, channel_title?}`.
  - `unconfigured` means no client ID or secret in config.
  - When connected, `channel_title` comes from `channels.list(mine=true, part=snippet)`. It is
    cached, and costs 1 quota unit.
- `DELETE /api/youtube/oauth` → revokes the token (`https://oauth2.googleapis.com/revoke`),
  deletes the file, and goes back to `disconnected`.
- `/api/status` gains `youtube_account: "unconfigured"|"disconnected"|"pending"|"connected"|"error"`.
- Access tokens refresh automatically from the refresh token. If Google says `invalid_grant`, the
  token was revoked or expired: delete the file, set the state to `disconnected`, and log a status
  event (component `youtube`, level `error`, message prefixed `OAuthRevoked:`).
- **Import (`import-account` and the daily 04:00 run):**
  - When connected, use the Data API with OAuth for: subscriptions
    (`subscriptions.list mine=true`, paged, into `account_imports` kind `subscription`), liked videos
    (`videos.list myRating=like`, paged, kind `liked`), and your playlists
    (`playlists.list mine=true` + `playlistItems.list`, kind `playlist`).
  - Watch history, recommendations and Watch Later still come from cookies via yt-dlp, but only
    when the cookies status is `ok`. Otherwise those kinds are skipped quietly, with an info event.
  - When OAuth is not connected, the old cookies path runs as today.
  - Quota: about 1 unit per page. Respect the existing quota ceiling.
- After the first successful OAuth import, seed or refresh the taste profile (existing
  `seed_taste_from_imports`, or a refresh if one exists).

## App behaviour (Taste tab)
A "YouTube account" card at the top of the Taste tab:
- **unconfigured:** "YouTube sign-in isn't set up on the Pi yet." (No action.)
- **disconnected:** "Connect your YouTube account to start from your subscriptions and likes."
  Button: **Connect YouTube**.
- **pending:**
  - The big `user_code` (monospace, selectable, with a "Copy code" button) and
    "Go to google.com/device and enter this code".
  - An **Open google.com/device** button that opens the browser on the phone.
  - "Waiting for approval…", polled every 3 s. It shows the expiry countdown, and offers Cancel
    once the code expires.
- **connected:** "Connected as <channel_title>". Button: **Disconnect**, with a confirm dialog.
- **error:** the message, plus **Try again**.

## Non-goals
Writing to YouTube (likes, playlists): read-only for now. Multi-account support.
