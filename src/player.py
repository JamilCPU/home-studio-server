"""Audio playback pipeline: yt-dlp → ffmpeg → Snapcast FIFO."""

from __future__ import annotations

import logging
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class PlayerProcess:
    """Holds references to the running yt-dlp/ffmpeg pipeline."""

    process: subprocess.Popen | None = None
    video_id: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def is_alive(self) -> bool:
        with self._lock:
            return self.process is not None and self.process.poll() is None


# Module-level singleton for current playback
_player = PlayerProcess()


def play_video(video_id: str, snap_fifo: str = "/tmp/snapfifo") -> bool:
    """Start streaming a YouTube video's audio to the Snapcast FIFO.

    Pipeline: yt-dlp (extract audio URL) → ffmpeg (decode → raw PCM) → FIFO
    """
    stop_playback()

    fifo_path = Path(snap_fifo)
    if not fifo_path.exists():
        log.error("Snapcast FIFO not found at %s — is Snapcast server running?", snap_fifo)
        return False

    url = f"https://www.youtube.com/watch?v={video_id}"

    try:
        # Get the best audio stream URL from yt-dlp
        audio_url = _get_audio_url(url)
        if not audio_url:
            return False

        # Stream audio → ffmpeg → PCM → FIFO
        cmd = [
            "ffmpeg",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-i", audio_url,
            "-f", "s16le",
            "-acodec", "pcm_s16le",
            "-ac", "2",
            "-ar", "48000",
            "-y",
            snap_fifo,
        ]

        with _player._lock:
            _player.process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            _player.video_id = video_id

        log.info("Started playback: %s (pid=%d)", video_id, _player.process.pid)
        return True

    except Exception as exc:
        log.error("Failed to start playback for %s: %s", video_id, exc)
        return False


def stop_playback() -> None:
    """Stop the current audio stream."""
    with _player._lock:
        if _player.process is not None:
            try:
                _player.process.terminate()
                _player.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _player.process.kill()
                _player.process.wait()
            except Exception as exc:
                log.warning("Error stopping playback: %s", exc)
            finally:
                log.info("Stopped playback: %s", _player.video_id)
                _player.process = None
                _player.video_id = ""


def get_current_video_id() -> str:
    """Return the video_id currently playing, or empty string."""
    if _player.is_alive():
        return _player.video_id
    return ""


def _get_audio_url(url: str) -> str | None:
    """Use yt-dlp to extract the best audio stream URL."""
    try:
        result = subprocess.run(
            [
                "yt-dlp",
                "--format", "bestaudio",
                "--get-url",
                "--no-warnings",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            log.error("yt-dlp failed: %s", result.stderr.strip())
            return None

        audio_url = result.stdout.strip()
        if not audio_url:
            log.error("yt-dlp returned empty URL for %s", url)
            return None

        return audio_url

    except subprocess.TimeoutExpired:
        log.error("yt-dlp timed out extracting URL for %s", url)
        return None
    except FileNotFoundError:
        log.error("yt-dlp not found — install with: pip install yt-dlp")
        return None


def precache_audio(video_id: str, cache_dir: Path = Path("data/audio_cache")) -> Path | None:
    """Download audio for a video to local cache for instant playback."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    output_path = cache_dir / f"{video_id}.opus"

    if output_path.exists():
        log.debug("Audio already cached: %s", output_path)
        return output_path

    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        result = subprocess.run(
            [
                "yt-dlp",
                "--format", "bestaudio",
                "--output", str(output_path),
                "--no-warnings",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0 and output_path.exists():
            log.info("Cached audio: %s", output_path)
            return output_path

        log.error("Failed to cache audio for %s: %s", video_id, result.stderr.strip())
        return None

    except subprocess.TimeoutExpired:
        log.error("yt-dlp timed out caching %s", video_id)
        return None


def play_cached(file_path: Path, snap_fifo: str = "/tmp/snapfifo") -> bool:
    """Play a pre-cached audio file through Snapcast."""
    stop_playback()

    if not file_path.exists():
        log.error("Cached file not found: %s", file_path)
        return False

    try:
        cmd = [
            "ffmpeg",
            "-i", str(file_path),
            "-f", "s16le",
            "-acodec", "pcm_s16le",
            "-ac", "2",
            "-ar", "48000",
            "-y",
            snap_fifo,
        ]

        with _player._lock:
            _player.process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            _player.video_id = file_path.stem

        log.info("Playing cached: %s (pid=%d)", file_path.name, _player.process.pid)
        return True

    except Exception as exc:
        log.error("Failed to play cached file %s: %s", file_path, exc)
        return False
