"""Wrapper around the ``claude`` CLI (subscription login, no API key).

Maps CLI failures to typed errors and extracts JSON from replies.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)

_RATE_LIMIT_MARKERS = ("usage limit", "rate limit")
_LOGIN_MARKERS = ("/login", "not logged in", "invalid api key")
_STDERR_SNIPPET = 200
_SHORT_REPLY_CHARS = 300
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
    runner: Callable[..., Any] = subprocess.run,
) -> str:
    """Run ``claude -p <prompt>`` and return stripped stdout."""
    try:
        result = runner(
            [command, "-p", prompt],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            encoding="utf-8",
        )
    except FileNotFoundError as exc:
        raise ClaudeNotFound(f"Claude CLI not found: {command}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ClaudeTimeout(f"Claude CLI timed out after {timeout_s}s") from exc

    stdout = result.stdout or ""
    stderr = result.stderr or ""

    # stderr is always checked. stdout is checked only when the exit code is
    # non-zero or the reply is a short one-liner (the CLI prints limit/login
    # messages that way, sometimes with exit 0); long replies are agent
    # output and may legitimately mention these phrases.
    scanned = stderr
    if result.returncode != 0 or len(stdout.strip()) < _SHORT_REPLY_CHARS:
        scanned = stdout + " " + stderr
    combined = scanned.lower()

    if any(m in combined for m in _RATE_LIMIT_MARKERS):
        raise ClaudeRateLimited("Claude usage or rate limit reached")
    if any(m in combined for m in _LOGIN_MARKERS):
        raise ClaudeNotLoggedIn("Claude CLI is not logged in")
    if result.returncode != 0:
        raise ClaudeError(
            f"Claude CLI failed (exit {result.returncode}): "
            f"{stderr.strip()[:_STDERR_SNIPPET]}"
        )

    output = stdout.strip()
    if not output:
        raise ClaudeBadOutput("empty response")
    logger.debug("Claude CLI response length: %d chars", len(output))
    return output


def make_runner(command: str = "claude", timeout_s: int = 300) -> ClaudeRunner:
    """Return a ``ClaudeRunner`` bound to *command* and *timeout_s*."""

    def runner(prompt: str) -> str:
        return run_claude(prompt, command=command, timeout_s=timeout_s)

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
