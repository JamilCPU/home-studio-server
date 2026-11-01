"""Wrapper around the ``claude`` CLI (subscription login, no API key).

Maps CLI failures to typed errors and extracts JSON from replies.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Callable, Protocol

from src.models import DEFAULT_CLAUDE_ARGS  # no tools, no MCP, no saved session

logger = logging.getLogger(__name__)

_RATE_LIMIT_MARKERS = ("usage limit", "rate limit")
_LOGIN_MARKERS = ("/login", "not logged in", "invalid api key")
_STDERR_SNIPPET = 200
_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL | re.IGNORECASE)


class ClaudeError(Exception):
    """Base class for Claude CLI failures."""


class ClaudeNotFound(ClaudeError):
    """The claude executable could not be found."""


class ClaudeNotLoggedIn(ClaudeError):
    """The claude CLI is not logged in."""


class ClaudeRateLimited(ClaudeError):
    """The subscription usage/rate limit was hit."""


class ClaudeTimeout(ClaudeError):
    """The claude CLI did not finish in time."""


class ClaudeBadOutput(ClaudeError):
    """The claude CLI returned empty or unparseable output."""


class ClaudeRunner(Protocol):
    """Callable that sends a prompt to Claude and returns its reply."""

    def __call__(self, prompt: str) -> str: ...


def run_claude(
    prompt: str,
    *,
    command: str = "claude",
    timeout_s: int = 300,
    cwd: Path | str | None = None,
    extra_args: Sequence[str] = (),
    runner: Callable[..., Any] | None = None,
) -> str:
    """Run ``claude -p <prompt> <extra_args>`` and return stripped stdout.

    It runs in ``cwd``, or else in a fresh empty temporary directory that is removed
    afterwards, never in the server's own directory (config and secrets live there).
    """
    runner = runner or subprocess.run
    if cwd is not None:
        return _run(prompt, command, timeout_s, str(cwd), extra_args, runner)
    with tempfile.TemporaryDirectory(prefix="home-studio-claude-") as empty:
        return _run(prompt, command, timeout_s, empty, extra_args, runner)


def _run(
    prompt: str,
    command: str,
    timeout_s: int,
    cwd: str,
    extra_args: Sequence[str],
    runner: Callable[..., Any],
) -> str:
    # The prompt goes right after -p: variadic options such as --tools would swallow it.
    try:
        result = runner(
            [command, "-p", prompt, *extra_args],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=timeout_s,
            encoding="utf-8",
            cwd=cwd,
        )
    except FileNotFoundError as exc:
        raise ClaudeNotFound(f"Claude CLI not found: {command}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ClaudeTimeout(f"Claude CLI timed out after {timeout_s}s") from exc
    stdout = result.stdout or ""
    _raise_for_markers(_scanned_text(result.returncode, stdout, result.stderr or ""))
    if result.returncode != 0:
        raise ClaudeError(
            f"Claude CLI failed (exit {result.returncode}): "
            f"{(result.stderr or '').strip()[:_STDERR_SNIPPET]}"
        )
    output = stdout.strip()
    if not output:
        raise ClaudeBadOutput("empty response")
    logger.debug("Claude CLI response length: %d chars", len(output))
    return output


def _parses(text: str) -> bool:
    try:
        extract_json(text)
    except ClaudeBadOutput:
        return False
    return True


def _scanned_text(returncode: int, stdout: str, stderr: str) -> str:
    """stderr is always checked for limit/login markers. stdout only when the exit code
    is non-zero or it holds no JSON (the CLI prints those messages as plain text,
    sometimes with exit 0); a JSON reply is agent output that may mention the phrases."""
    if returncode == 0 and _parses(stdout):
        return stderr.lower()
    return (stdout + " " + stderr).lower()


def _raise_for_markers(text: str) -> None:
    if any(m in text for m in _RATE_LIMIT_MARKERS):
        raise ClaudeRateLimited("Claude usage or rate limit reached")
    if any(m in text for m in _LOGIN_MARKERS):
        raise ClaudeNotLoggedIn("Claude CLI is not logged in")


def make_runner(
    command: str = "claude",
    timeout_s: int = 300,
    extra_args: Sequence[str] = DEFAULT_CLAUDE_ARGS,
) -> ClaudeRunner:
    """Return a ``ClaudeRunner`` bound to *command*, *timeout_s* and *extra_args*."""

    def runner(prompt: str) -> str:
        return run_claude(prompt, command=command, timeout_s=timeout_s, extra_args=extra_args)

    return runner


def _balanced_end(text: str, start: int) -> int | None:
    """Return the index just past the span balanced from *start*, or None."""
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def extract_json(text: str) -> dict | list:
    """Parse JSON from a Claude reply.

    Tries a fenced block first, then the first balanced ``{...}``/``[...]``
    span that parses. Raises ``ClaudeBadOutput`` if nothing parses.
    """
    fence = _FENCE_RE.search(text)
    if fence:
        try:
            parsed = json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, (dict, list)):
            return parsed

    for start, ch in enumerate(text):
        if ch not in "{[":
            continue
        end = _balanced_end(text, start)
        if end is None:
            continue
        try:
            parsed = json.loads(text[start:end])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, (dict, list)):
            return parsed

    raise ClaudeBadOutput("no JSON object or array found in response")
