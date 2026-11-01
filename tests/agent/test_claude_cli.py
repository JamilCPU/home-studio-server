"""Tests for the Claude CLI wrapper."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from src.agent.claude_cli import (
    ClaudeBadOutput,
    ClaudeError,
    ClaudeNotFound,
    ClaudeNotLoggedIn,
    ClaudeRateLimited,
    ClaudeTimeout,
    extract_json,
    make_runner,
    run_claude,
)
from tests.agent.fakes import FakeClaude


def _runner(stdout="", stderr="", returncode=0, calls=None):
    def run(cmd, **kwargs):
        if calls is not None:
            calls.append((cmd, kwargs))
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)

    return run


def _raising(exc):
    def run(cmd, **kwargs):
        raise exc

    return run


def test_run_claude_returns_stripped_stdout_and_passes_args():
    calls = []
    out = run_claude("hi", command="cl", timeout_s=7, runner=_runner("  ok \n", calls=calls))
    assert out == "ok"
    cmd, kwargs = calls[0]
    assert cmd == ["cl", "-p", "hi"]
    assert kwargs == {
        "capture_output": True,
        "text": True,
        "timeout": 7,
        "encoding": "utf-8",
    }


def test_missing_binary_maps_to_not_found():
    with pytest.raises(ClaudeNotFound):
        run_claude("x", runner=_raising(FileNotFoundError("claude")))


def test_timeout_maps_to_claude_timeout():
    with pytest.raises(ClaudeTimeout):
        run_claude("x", runner=_raising(subprocess.TimeoutExpired("claude", 1)))


@pytest.mark.parametrize("marker", ["Usage limit reached", "Rate limit exceeded"])
def test_rate_limit_markers_in_stdout_even_with_exit_zero(marker):
    with pytest.raises(ClaudeRateLimited):
        run_claude("x", runner=_runner(stdout=marker, returncode=0))


def test_rate_limit_marker_in_stderr_with_nonzero_exit():
    with pytest.raises(ClaudeRateLimited):
        run_claude("x", runner=_runner(stderr="5-hour USAGE LIMIT hit", returncode=1))


@pytest.mark.parametrize("marker", ["Please run /login", "Not logged in", "Invalid API key"])
def test_login_markers_map_to_not_logged_in(marker):
    with pytest.raises(ClaudeNotLoggedIn):
        run_claude("x", runner=_runner(stdout=marker, returncode=0))


def test_nonzero_exit_without_marker_is_generic_error():
    with pytest.raises(ClaudeError) as info:
        run_claude("x", runner=_runner(stderr="e" * 500, returncode=2))
    assert type(info.value) is ClaudeError
    assert str(info.value).count("e") >= 200
    assert "e" * 201 not in str(info.value)


def test_empty_stdout_is_bad_output():
    with pytest.raises(ClaudeBadOutput, match="empty response"):
        run_claude("x", runner=_runner(stdout="  \n"))


def test_all_errors_subclass_claude_error():
    for cls in (ClaudeNotFound, ClaudeNotLoggedIn, ClaudeRateLimited, ClaudeTimeout, ClaudeBadOutput):
        assert issubclass(cls, ClaudeError)


def test_make_runner_binds_command_and_timeout(monkeypatch):
    seen = {}

    def fake_run(prompt, *, command, timeout_s):
        seen.update(prompt=prompt, command=command, timeout_s=timeout_s)
        return "done"

    monkeypatch.setattr("src.agent.claude_cli.run_claude", fake_run)
    assert make_runner(command="c", timeout_s=9)("p") == "done"
    assert seen == {"prompt": "p", "command": "c", "timeout_s": 9}


def test_extract_json_fenced():
    assert extract_json('Sure:\n```json\n{"a": 1}\n```\nbye') == {"a": 1}


def test_extract_json_bare_fence():
    assert extract_json("```\n[1, 2]\n```") == [1, 2]


def test_extract_json_bare_object():
    assert extract_json('Here you go {"a": {"b": 2}} done') == {"a": {"b": 2}}


def test_extract_json_bare_array():
    assert extract_json("result: [1, 2, 3]") == [1, 2, 3]


def test_extract_json_braces_inside_strings():
    assert extract_json('x {"a": "}{ \\" ]"} y') == {"a": '}{ " ]'}


def test_extract_json_skips_unparseable_prefix_brace():
    assert extract_json('note {not json} then {"ok": true}') == {"ok": True}


def test_extract_json_garbage_raises():
    with pytest.raises(ClaudeBadOutput):
        extract_json("no json here at all")


def test_extract_json_unbalanced_raises():
    with pytest.raises(ClaudeBadOutput):
        extract_json('{"a": 1')


def test_fake_claude_scripts_and_records():
    fake = FakeClaude(["one", ClaudeTimeout("t")])
    assert fake("p1") == "one"
    with pytest.raises(ClaudeTimeout):
        fake("p2")
    assert fake.prompts == ["p1", "p2"]
    with pytest.raises(AssertionError):
        fake("p3")


def test_long_json_stdout_mentioning_rate_limit_is_returned_unchanged():
    body = '{"reason": "covers API rate limit design", "pad": "' + "x" * 300 + '"}'
    assert run_claude("x", runner=_runner(stdout=body, returncode=0)) == body


def test_short_usage_limit_stdout_with_exit_zero_still_raises():
    with pytest.raises(ClaudeRateLimited):
        run_claude("x", runner=_runner(stdout="Claude usage limit reached. Resets at 5pm."))


def test_stderr_marker_with_exit_zero_still_raises():
    long_ok = "y" * 400
    with pytest.raises(ClaudeRateLimited):
        run_claude("x", runner=_runner(stdout=long_ok, stderr="rate limit", returncode=0))
