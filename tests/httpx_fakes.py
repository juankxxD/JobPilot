"""Tiny httpx stand-ins for fetcher tests (no network)."""

from __future__ import annotations

from typing import Any


class FakeResponse:
    def __init__(self, *, json_data: Any = None, text: str = "", status_code: int = 200) -> None:
        self._json = json_data
        self.text = text
        self.status_code = status_code

    def json(self) -> Any:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    """A client whose ``get`` always returns the configured response."""

    def __init__(self, response: FakeResponse) -> None:
        self._response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        return self._response

    def close(self) -> None:
        pass


class RaisingClient:
    """A client whose ``get`` raises, to exercise error isolation."""

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error or RuntimeError("boom")

    def get(self, url: str, **kwargs: Any) -> Any:
        raise self._error

    def close(self) -> None:
        pass
