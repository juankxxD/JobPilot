"""TelegramNotifier: proposal payload, owner filtering and update parsing.

The Telegram HTTP client is faked, so no network is touched and no token is used.
"""

from __future__ import annotations

from typing import Any

from jobpilot.channels.telegram import TelegramNotifier, proposal_buttons
from jobpilot.config import Language
from jobpilot.models import Attachment, Job, JobStatus

OWNER = "123456"


class _Resp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeHttp:
    """Records every POST and returns queued (or default ok) responses."""

    def __init__(self, responses: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._responses = responses or []

    def post(self, url: str, **kwargs: Any) -> _Resp:
        self.calls.append((url, kwargs))
        if self._responses:
            return _Resp(self._responses.pop(0))
        return _Resp({"ok": True, "result": {}})


def _job() -> Job:
    return Job(
        id=42,
        source="remoteok",
        external_id="rok-42",
        url="https://example.com/job/42",
        apply_url="https://example.com/apply/42",
        title="Backend Engineer",
        company="ACME Corp",
        language=Language.en,
        score=88,
        score_reason="Strong Java and AWS match",
        missing_requirements=["Kotlin"],
        status=JobStatus.proposed,
    )


def _notifier(http: FakeHttp) -> TelegramNotifier:
    return TelegramNotifier(bot_token="token", owner_chat_id=OWNER, http=http)


def test_send_proposal_includes_six_buttons_and_attachments() -> None:
    http = FakeHttp()
    notifier = _notifier(http)
    cv = Attachment(filename="cv_EN.pdf", content=b"%PDF cv")
    cover = Attachment(filename="cover_EN.pdf", content=b"%PDF cover")

    notifier.send_proposal(_job(), attachments=[cv, cover])

    # Two sendDocument calls (cv + cover) then one sendMessage with the keyboard.
    methods = [url.rsplit("/", 1)[-1] for url, _ in http.calls]
    assert methods == ["sendDocument", "sendDocument", "sendMessage"]

    message_payload = http.calls[-1][1]["json"]
    keyboard = message_payload["reply_markup"]["inline_keyboard"]
    labels = [row[0]["text"] for row in keyboard]
    assert labels == ["Enviar", "Ajustar", "Descartar", "Ya apliqué", "Español", "Inglés"]
    callbacks = [row[0]["callback_data"] for row in keyboard]
    assert callbacks == [b.callback_data for b in proposal_buttons(42)]
    # Spanish copy carries the offer facts.
    assert "ACME Corp" in message_payload["text"]
    assert "Backend Engineer" in message_payload["text"]


def test_get_updates_drops_non_owner_chats() -> None:
    result = [
        {
            "update_id": 10,
            "message": {"chat": {"id": 999}, "text": "/pendientes"},
        },
        {
            "update_id": 11,
            "message": {"chat": {"id": int(OWNER)}, "text": "/estado"},
        },
    ]
    http = FakeHttp([{"ok": True, "result": result}])
    notifier = _notifier(http)

    updates = notifier.get_updates()

    assert [u.update_id for u in updates] == [11]
    assert updates[0].chat_id == OWNER
    assert updates[0].text == "/estado"


def test_get_updates_parses_owner_callback_and_passes_offset() -> None:
    result = [
        {
            "update_id": 20,
            "callback_query": {
                "data": "send:42",
                "message": {"chat": {"id": int(OWNER)}},
            },
        }
    ]
    http = FakeHttp([{"ok": True, "result": result}])
    notifier = _notifier(http)

    updates = notifier.get_updates(offset=15)

    assert http.calls[0][1]["json"]["offset"] == 15
    assert len(updates) == 1
    assert updates[0].callback_data == "send:42"
    assert updates[0].chat_id == OWNER
