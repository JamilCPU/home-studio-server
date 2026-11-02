"""Tests for the mpv command line (no real mpv is started)."""

from __future__ import annotations

from pathlib import Path

from src.player.mpv import mpv_argv, venv_ytdlp


def test_argv_points_ytdl_hook_at_given_ytdlp(tmp_path):
    ytdlp = tmp_path / "yt-dlp"
    argv = mpv_argv(Path("/tmp/s.sock"), Path("c.txt"), ytdlp_path=ytdlp)
    assert f"--script-opts=ytdl_hook-ytdl_path={ytdlp}" in argv
    assert argv[0] == "mpv"
    assert "--ytdl-raw-options=cookies=c.txt" in argv


def test_argv_without_ytdlp_has_no_script_opt():
    argv = mpv_argv(Path("/tmp/s.sock"), Path("c.txt"), ytdlp_path=None)
    assert not any(a.startswith("--script-opts") for a in argv)


def test_venv_ytdlp_found_next_to_python(tmp_path):
    python = tmp_path / "bin" / "python"
    python.parent.mkdir()
    python.write_text("")
    (tmp_path / "bin" / "yt-dlp").write_text("")
    assert venv_ytdlp(python) == tmp_path / "bin" / "yt-dlp"


def test_venv_ytdlp_none_when_missing(tmp_path):
    assert venv_ytdlp(tmp_path / "python") is None
