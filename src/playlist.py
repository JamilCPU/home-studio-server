"""Manage the YouTube 'Morning Feed' playlist via YouTube Data API v3."""

from __future__ import annotations

import logging

from googleapiclient.discovery import Resource
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)


def get_or_create_playlist(
    youtube: Resource,
    title: str = "Morning Feed",
) -> str:
    """Return the playlist ID for *title*, creating it if it does not exist.

    Searches the authenticated user's playlists for one matching *title*.
    If none is found a new **private** playlist is created.
    """
    try:
        response = (
            youtube.playlists()
            .list(part="snippet", mine=True, maxResults=50)
            .execute()
        )
    except HttpError as exc:
        logger.error("Failed to list playlists: %s", exc)
        raise

    for item in response.get("items", []):
        if item["snippet"]["title"] == title:
            playlist_id: str = item["id"]
            logger.info("Found existing playlist '%s' (%s)", title, playlist_id)
            return playlist_id

    # Playlist does not exist — create it
    logger.info("Playlist '%s' not found — creating a new one", title)
    try:
        created = (
            youtube.playlists()
            .insert(
                part="snippet,status",
                body={
                    "snippet": {
                        "title": title,
                        "description": "Auto-curated by YoutubeBrainwash",
                    },
                    "status": {"privacyStatus": "private"},
                },
            )
            .execute()
        )
    except HttpError as exc:
        logger.error("Failed to create playlist '%s': %s", title, exc)
        raise

    new_id: str = created["id"]
    logger.info("Created playlist '%s' (%s)", title, new_id)
    return new_id


def clear_playlist(youtube: Resource, playlist_id: str) -> int:
    """Remove all items from *playlist_id* and return the number removed."""
    removed = 0

    while True:
        try:
            response = (
                youtube.playlistItems()
                .list(part="id", playlistId=playlist_id, maxResults=50)
                .execute()
            )
        except HttpError as exc:
            logger.error("Failed to list playlist items: %s", exc)
            raise

        items = response.get("items", [])
        if not items:
            break

        for item in items:
            item_id: str = item["id"]
            try:
                youtube.playlistItems().delete(id=item_id).execute()
                removed += 1
            except HttpError as exc:
                logger.warning("Failed to delete playlist item %s: %s", item_id, exc)

    logger.info("Cleared %d items from playlist %s", removed, playlist_id)
    return removed


def update_playlist(
    youtube: Resource,
    playlist_id: str,
    video_ids: list[str],
) -> None:
    """Replace the contents of *playlist_id* with *video_ids* in order.

    All existing items are removed first, then the new videos are inserted
    sequentially so the playlist order matches the list order.
    """
    cleared = clear_playlist(youtube, playlist_id)
    logger.info("Cleared %d existing items before update", cleared)

    for position, vid in enumerate(video_ids):
        try:
            youtube.playlistItems().insert(
                part="snippet",
                body={
                    "snippet": {
                        "playlistId": playlist_id,
                        "resourceId": {
                            "kind": "youtube#video",
                            "videoId": vid,
                        },
                    },
                },
            ).execute()
            logger.debug("Inserted video %s at position %d", vid, position)
        except HttpError as exc:
            logger.error(
                "Failed to insert video %s into playlist %s: %s",
                vid,
                playlist_id,
                exc,
            )
            # Continue with remaining videos rather than aborting entirely
            continue

    logger.info(
        "Updated playlist %s with %d videos",
        playlist_id,
        len(video_ids),
    )
