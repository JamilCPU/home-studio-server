"""YoutubeBrainwash CLI — daily curation orchestrator."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from src.config import load_config

log = logging.getLogger("brainwash")

CURRENT_FEED_PATH = Path("data/current_feed.json")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="brainwash",
        description="YoutubeBrainwash — Personalized YouTube feed curator",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # curate — run the full curation pipeline
    subparsers.add_parser("curate", help="Run the full curation pipeline")

    # serve — start the FastAPI server
    serve_parser = subparsers.add_parser("serve", help="Start the REST API server")
    serve_parser.add_argument("--host", default=None, help="Override API host")
    serve_parser.add_argument("--port", type=int, default=None, help="Override API port")

    # play — start playback of the current feed
    play_parser = subparsers.add_parser("play", help="Start playing the current feed")
    play_parser.add_argument("--video-id", default=None, help="Play a specific video")

    # stop — stop playback
    subparsers.add_parser("stop", help="Stop current playback")

    # feed — show the current feed
    subparsers.add_parser("feed", help="Show today's curated feed")

    # tune — auto-tune the interest profile from feedback
    subparsers.add_parser("tune", help="Auto-tune interest profile from feedback")

    # import-history — import Google Takeout watch history
    import_parser = subparsers.add_parser(
        "import-history", help="Import watch history from Google Takeout"
    )
    import_parser.add_argument("file", help="Path to Takeout watch-history.json")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    config = load_config()

    if args.command == "curate":
        _cmd_curate(config)
    elif args.command == "serve":
        _cmd_serve(config, args)
    elif args.command == "play":
        _cmd_play(config, args)
    elif args.command == "stop":
        _cmd_stop()
    elif args.command == "feed":
        _cmd_feed()
    elif args.command == "tune":
        _cmd_tune()
    elif args.command == "import-history":
        _cmd_import_history(args.file)


def _cmd_curate(config) -> None:
    """Run the full daily curation pipeline."""
    from src.analyzer import score_videos
    from src.auth import get_youtube_service
    from src.fetcher import (
        deduplicate_candidates,
        fetch_liked_videos,
        fetch_search_results,
        fetch_subscription_videos,
        filter_candidates,
    )
    from src.playlist import get_or_create_playlist, update_playlist
    from src.profile import append_score_log, get_feedback_summary, load_feedback, load_profile
    from src.transcript import fetch_transcripts_batch, truncate_transcript

    log.info("=== YoutubeBrainwash Curation Run ===")
    log.info("Time: %s", datetime.now(timezone.utc).isoformat())

    # Step 1: Authenticate
    log.info("Step 1: Authenticating with YouTube API...")
    youtube = get_youtube_service(config)

    # Step 2: Gather candidates
    log.info("Step 2: Gathering video candidates...")
    candidates = []
    candidates.extend(fetch_subscription_videos(youtube, recency_hours=config.recency_hours))
    candidates.extend(fetch_search_results(youtube, queries=list(config.search_topics)))
    candidates = deduplicate_candidates(candidates)
    candidates = filter_candidates(
        candidates,
        min_duration_minutes=config.min_duration_minutes,
        max_duration_minutes=config.max_duration_minutes,
        language=config.language,
    )
    log.info("Candidates after filtering: %d", len(candidates))

    # Step 3: Select top candidates for transcript fetch
    # Sort by view count (descending) and take top N
    candidates.sort(key=lambda v: v.view_count, reverse=True)
    transcript_candidates = candidates[: config.transcript_candidates]
    log.info(
        "Step 3: Fetching transcripts for top %d candidates...",
        len(transcript_candidates),
    )
    video_ids = [v.video_id for v in transcript_candidates]
    transcripts = fetch_transcripts_batch(video_ids, language=config.language)

    # Truncate transcripts for prompt size
    from dataclasses import replace

    for vid, tr in transcripts.items():
        transcripts[vid] = replace(tr, text=truncate_transcript(tr.text))

    # Step 4: Load profile + feedback context
    log.info("Step 4: Loading interest profile and feedback...")
    profile = load_profile()
    feedback = load_feedback()
    feedback_summary = get_feedback_summary(feedback)
    profile_with_feedback = {**profile, "recent_feedback": feedback_summary}

    # Step 5: Score with Claude
    log.info("Step 5: Scoring videos with Claude...")
    scored = score_videos(transcript_candidates, transcripts, profile_with_feedback, config)
    feed = [v for v in scored if v.score >= config.min_score]
    feed = feed[: config.max_feed_size]
    log.info("Feed: %d videos scored >= %.1f", len(feed), config.min_score)

    # Step 6: Save feed
    feed_data = [
        {
            "video_id": v.video_id,
            "title": v.title,
            "channel_name": v.channel_name,
            "score": v.score,
            "reason": v.reason,
            "duration_seconds": v.duration_seconds,
            "thumbnail_url": v.thumbnail_url,
            "youtube_url": f"https://www.youtube.com/watch?v={v.video_id}",
            "scored_at": v.scored_at.isoformat(),
        }
        for v in feed
    ]
    CURRENT_FEED_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CURRENT_FEED_PATH, "w") as f:
        json.dump(feed_data, f, indent=2)
    log.info("Saved feed to %s", CURRENT_FEED_PATH)

    # Step 7: Log scores
    append_score_log(feed_data)

    # Step 8: Update YouTube playlist
    log.info("Step 8: Updating Morning Feed playlist...")
    try:
        playlist_id = get_or_create_playlist(youtube)
        update_playlist(youtube, playlist_id, [v.video_id for v in feed])
        log.info("Updated playlist: %d videos", len(feed))
    except Exception as exc:
        log.error("Failed to update playlist: %s", exc)

    log.info("=== Curation complete: %d videos in today's feed ===", len(feed))


def _cmd_serve(config, args) -> None:
    """Removed; replaced by the new server."""
    _removed()


def _cmd_play(config, args) -> None:
    """Removed; replaced by the new server."""
    _removed()


def _cmd_stop() -> None:
    """Removed; replaced by the new server."""
    _removed()


def _removed() -> None:
    log.error("This command was removed; replaced by the new server")
    sys.exit(1)


def _cmd_feed() -> None:
    """Display the current feed."""
    if not CURRENT_FEED_PATH.exists():
        log.error("No feed available. Run 'brainwash curate' first.")
        sys.exit(1)

    with open(CURRENT_FEED_PATH) as f:
        feed = json.load(f)

    if not feed:
        print("Feed is empty.")
        return

    print(f"\n{'='*60}")
    print(f"  Morning Feed — {len(feed)} videos")
    print(f"{'='*60}\n")
    for i, v in enumerate(feed, 1):
        mins = v.get("duration_seconds", 0) // 60
        print(f"  {i:2d}. [{v['score']:.1f}] {v['title']}")
        print(f"      {v['channel_name']} · {mins}min")
        print(f"      {v['reason']}")
        print()


def _cmd_tune() -> None:
    """Auto-tune interest profile from recent feedback."""
    from src.profile import auto_tune_profile, load_feedback, load_profile, save_profile

    profile = load_profile()
    feedback = load_feedback()

    new_profile = auto_tune_profile(profile, feedback)
    save_profile(new_profile)
    log.info("Profile auto-tuned from %d feedback entries", len(feedback))


def _cmd_import_history(file_path: str) -> None:
    """Import watch history from a Google Takeout JSON file."""
    source = Path(file_path)
    if not source.exists():
        log.error("File not found: %s", source)
        sys.exit(1)

    dest = Path("data/watch_history.json")
    dest.parent.mkdir(parents=True, exist_ok=True)

    with open(source) as f:
        raw = json.load(f)

    # Google Takeout format: list of dicts with "title", "titleUrl", "time"
    history = []
    for entry in raw:
        title_url = entry.get("titleUrl", "")
        if "youtube.com/watch" not in title_url:
            continue
        video_id = title_url.split("v=")[-1] if "v=" in title_url else ""
        if not video_id:
            continue
        history.append(
            {
                "video_id": video_id,
                "title": entry.get("title", "").removeprefix("Watched "),
                "watched_at": entry.get("time", ""),
            }
        )

    with open(dest, "w") as f:
        json.dump(history, f, indent=2)

    log.info("Imported %d watch history entries to %s", len(history), dest)


if __name__ == "__main__":
    main()
