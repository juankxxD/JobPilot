"""Parsing of Telegram commands and inline-button callbacks into intents."""

from __future__ import annotations

import pytest

from jobpilot.channels.telegram import CallbackAction as CA
from jobpilot.channels.telegram import IntentKind, encode_callback, parse_intent
from jobpilot.config import Language
from jobpilot.interfaces import Update


def _text_update(text: str, update_id: int = 1) -> Update:
    return Update(update_id=update_id, chat_id="1", text=text)


def _callback_update(data: str, update_id: int = 1) -> Update:
    return Update(update_id=update_id, chat_id="1", callback_data=data)


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("/pendientes", IntentKind.list_pending),
        ("/estado", IntentKind.status),
        ("/buscar", IntentKind.search),
        ("/buscar backend remoto", IntentKind.search),
    ],
)
def test_simple_commands(text: str, kind: IntentKind) -> None:
    intent = parse_intent(_text_update(text))
    assert intent.kind is kind


def test_search_captures_query() -> None:
    intent = parse_intent(_text_update("/buscar backend remoto"))
    assert intent.kind is IntentKind.search
    assert intent.text == "backend remoto"


def test_entrevista_and_rechazo_capture_job_id() -> None:
    interview = parse_intent(_text_update("/entrevista 7"))
    assert interview.kind is IntentKind.mark_interview
    assert interview.job_id == 7

    rejected = parse_intent(_text_update("/rechazo 9"))
    assert rejected.kind is IntentKind.mark_rejected
    assert rejected.job_id == 9


def test_id_command_without_number_is_unknown() -> None:
    intent = parse_intent(_text_update("/entrevista"))
    assert intent.kind is IntentKind.unknown


@pytest.mark.parametrize(
    ("action", "kind"),
    [
        (CA.send, IntentKind.send),
        (CA.adjust, IntentKind.adjust),
        (CA.discard, IntentKind.discard),
        (CA.applied, IntentKind.mark_applied),
    ],
)
def test_action_callbacks(action: CA, kind: IntentKind) -> None:
    intent = parse_intent(_callback_update(encode_callback(action, 5)))
    assert intent.kind is kind
    assert intent.job_id == 5


def test_language_callbacks_carry_language() -> None:
    es = parse_intent(_callback_update(encode_callback(CA.lang_es, 3)))
    assert es.kind is IntentKind.choose_language
    assert es.language is Language.es
    assert es.job_id == 3

    en = parse_intent(_callback_update(encode_callback(CA.lang_en, 3)))
    assert en.kind is IntentKind.choose_language
    assert en.language is Language.en


def test_free_text_intent() -> None:
    intent = parse_intent(_text_update("usa un tono más formal"))
    assert intent.kind is IntentKind.free_text
    assert intent.text == "usa un tono más formal"


def test_unknown_callback_is_handled_gracefully() -> None:
    assert parse_intent(_callback_update("nonsense")).kind is IntentKind.unknown
    assert parse_intent(_callback_update("send:notanumber")).kind is IntentKind.unknown
    assert parse_intent(_text_update("")).kind is IntentKind.unknown
