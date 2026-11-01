"""JSON shapes the API returns (see docs/api.md); converters from store records to dicts."""

from __future__ import annotations

from typing import Any

from src.player.engine import NowPlaying
from src.store.agent import AgentRequest, TasteProfile
from src.store.queue import QueueItem
from src.store.settings import MorningSettings
from src.store.videos import HistoryEntry, Rating, Video


def video_out(v: Video) -> dict[str, Any]:
    return {
        "video_id": v.video_id,
        "title": v.title,
        "channel": v.channel_name,
        "thumbnail_url": v.thumbnail_url,
        "duration_s": v.duration_s,
        "chapters": [{"title": c.title, "start_s": c.start_s} for c in v.chapters],
    }


def queue_item_out(item: QueueItem) -> dict[str, Any]:
    video = video_out(item.video)
    return {
        "id": item.id,
        "video_id": video["video_id"],
        "title": video["title"],
        "channel": video["channel"],
        "thumbnail_url": video["thumbnail_url"],
        "duration_s": video["duration_s"],
        "source": item.source,
        "reason": item.reason,
        "chapters": video["chapters"],
    }


def now_out(now: NowPlaying, output_device: str) -> dict[str, Any]:
    return {
        "state": now.state,
        "item": queue_item_out(now.item) if now.item is not None else None,
        "position_s": float(now.position_s),
        "duration_s": float(now.duration_s) if now.duration_s is not None else None,
        "output_device": output_device,
    }


def rating_out(r: Rating | None) -> dict[str, Any] | None:
    if r is None:
        return None
    return {"liked": r.liked, "satisfaction": r.satisfaction, "notes": r.notes,
            "created_at": r.created_at}


def history_out(e: HistoryEntry) -> dict[str, Any]:
    return {
        "play_id": e.play_id,
        "video": video_out(e.video),
        "started_at": e.started_at,
        "seconds_listened": e.seconds_listened,
        "end_reason": e.end_reason,
        "source": e.source,
        "rating": rating_out(e.latest_rating),
    }


def taste_out(t: TasteProfile | None) -> dict[str, Any] | None:
    if t is None:
        return None
    return {"summary": t.summary, "likes": list(t.likes), "dislikes": list(t.dislikes),
            "updated_at": t.created_at}


def request_out(r: AgentRequest, items: list[QueueItem]) -> dict[str, Any]:
    return {
        "id": r.id,
        "prompt": r.prompt,
        "status": r.status,
        "summary": r.summary,
        "error": r.error,
        "items": [queue_item_out(i) for i in items],
        "created_at": r.created_at,
        "finished_at": r.finished_at,
    }


def morning_out(s: MorningSettings) -> dict[str, Any]:
    return {"enabled": s.enabled, "wake_time": s.wake_time, "days": list(s.days),
            "topics": list(s.topics), "count": s.count}
