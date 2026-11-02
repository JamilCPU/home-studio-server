"""Test doubles for the agent package."""

from __future__ import annotations


class FakeClaude:
    """Scripted ``ClaudeRunner``: returns or raises responses in order."""

    def __init__(self, responses: list[str | Exception]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        index = len(self.prompts) - 1
        if index >= len(self._responses):
            raise AssertionError(
                f"FakeClaude called {len(self.prompts)} times but only "
                f"{len(self._responses)} responses were scripted"
            )
        response = self._responses[index]
        if isinstance(response, Exception):
            raise response
        return response
