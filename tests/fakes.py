"""Fakes implementing the project's Protocols, for use in tests."""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class FakeLlmClient:
    """An LlmClient that returns a pre-built schema instance and records prompts."""

    def __init__(self, response: BaseModel) -> None:
        self._response = response
        self.prompts: list[str] = []

    def complete_json(self, prompt: str, schema: type[SchemaT], *, max_retries: int = 1) -> SchemaT:
        self.prompts.append(prompt)
        if not isinstance(self._response, schema):
            raise AssertionError(
                f"FakeLlmClient was asked for {schema.__name__} but holds "
                f"{type(self._response).__name__}"
            )
        return self._response
