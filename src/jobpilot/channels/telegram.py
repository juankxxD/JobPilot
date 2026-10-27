"""Telegram channel: the human-in-the-loop approval surface.

``TelegramNotifier`` implements the :class:`~jobpilot.interfaces.Notifier`
Protocol over the Telegram Bot API. It:

* sends a proposal message (title/company/score/language/summary/missing/offer-link)
  with the CV and cover-letter documents attached and an inline keyboard whose
  callbacks encode ``action:job_id`` (Enviar / Ajustar / Descartar / Ya apliqué, and
  the Español / Inglés language choice);
* reads updates via ``getUpdates`` and normalises them to :class:`Update`,
  dropping anything whose chat is not the configured ``TELEGRAM_OWNER_CHAT_ID`` so
  the bot only ever obeys its owner.

Parsing of commands and inline-button callbacks into structured
:class:`TelegramIntent` values lives here too, so the I/O-free dispatch logic is
unit-testable and the pipeline only has to act on intents.

All user-facing strings are in Spanish; code, identifiers and logs are in English.
The HTTP client is injected so tests never touch the network.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel

from jobpilot.config import Language, Settings, get_settings
from jobpilot.interfaces import Button, Update, UpdateBatch
from jobpilot.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from jobpilot.models import Attachment, Job

log = get_logger(__name__)

_API_BASE = "https://api.telegram.org"


class TelegramDeliveryError(RuntimeError):
    """Raised when the Telegram Bot API rejects a send (``ok: false``).

    The caller must not treat a rejected send as delivered: a proposal whose
    message or document never reached the owner must not advance to ``proposed``,
    so the job is retried on a later run instead of being silently skipped.
    """


# --------------------------------------------------------------------------- #
# Inline-button actions and their Spanish labels
# --------------------------------------------------------------------------- #
class CallbackAction(StrEnum):
    """The action encoded in an inline-button ``callback_data`` (``action:job_id``)."""

    send = "send"
    adjust = "adjust"
    discard = "discard"
    applied = "applied"
    lang_es = "lang_es"
    lang_en = "lang_en"


# Spanish labels shown on the inline keyboard. English identifiers, Spanish text.
_ACTION_LABELS: dict[CallbackAction, str] = {
    CallbackAction.send: "Enviar",
    CallbackAction.adjust: "Ajustar",
    CallbackAction.discard: "Descartar",
    CallbackAction.applied: "Ya apliqué",
    CallbackAction.lang_es: "Español",
    CallbackAction.lang_en: "Inglés",
}

# The six buttons shown with a full proposal.
_PROPOSAL_ACTIONS: tuple[CallbackAction, ...] = (
    CallbackAction.send,
    CallbackAction.adjust,
    CallbackAction.discard,
    CallbackAction.applied,
    CallbackAction.lang_es,
    CallbackAction.lang_en,
)

# The three buttons shown when the offer language is still undecided.
_LANGUAGE_ACTIONS: tuple[CallbackAction, ...] = (
    CallbackAction.lang_es,
    CallbackAction.lang_en,
    CallbackAction.discard,
)


def encode_callback(action: CallbackAction, job_id: int) -> str:
    """Encode an inline-button callback as ``action:job_id``."""
    return f"{action.value}:{job_id}"


def _build_buttons(actions: Sequence[CallbackAction], job_id: int) -> list[Button]:
    return [
        Button(text=_ACTION_LABELS[action], callback_data=encode_callback(action, job_id))
        for action in actions
    ]


def proposal_buttons(job_id: int) -> list[Button]:
    """The six inline buttons attached to a full proposal."""
    return _build_buttons(_PROPOSAL_ACTIONS, job_id)


def language_buttons(job_id: int) -> list[Button]:
    """The three inline buttons for the ambiguous-language prompt."""
    return _build_buttons(_LANGUAGE_ACTIONS, job_id)


# --------------------------------------------------------------------------- #
# Structured intents parsed from updates
# --------------------------------------------------------------------------- #
class IntentKind(StrEnum):
    """The kind of action an :class:`Update` asks the pipeline to take."""

    # Inline-button callbacks.
    send = "send"
    adjust = "adjust"
    discard = "discard"
    mark_applied = "mark_applied"
    choose_language = "choose_language"
    # Text commands.
    list_pending = "list_pending"
    status = "status"
    mark_interview = "mark_interview"
    mark_rejected = "mark_rejected"
    search = "search"
    # Free text that is not a command (e.g. the Ajustar instruction).
    free_text = "free_text"
    # Anything we do not recognise.
    unknown = "unknown"


class TelegramIntent(BaseModel):
    """A parsed, I/O-free instruction derived from a single :class:`Update`.

    ``job_id`` is populated for callbacks and for the ``/entrevista``/``/rechazo``
    commands; ``language`` only for a language choice; ``text`` carries the free-text
    payload (for example the Ajustar instruction or a ``/buscar`` query).
    """

    kind: IntentKind
    update_id: int
    job_id: int | None = None
    language: Language | None = None
    text: str | None = None


_CALLBACK_INTENT: dict[CallbackAction, IntentKind] = {
    CallbackAction.send: IntentKind.send,
    CallbackAction.adjust: IntentKind.adjust,
    CallbackAction.discard: IntentKind.discard,
    CallbackAction.applied: IntentKind.mark_applied,
    CallbackAction.lang_es: IntentKind.choose_language,
    CallbackAction.lang_en: IntentKind.choose_language,
}

_CALLBACK_LANGUAGE: dict[CallbackAction, Language] = {
    CallbackAction.lang_es: Language.es,
    CallbackAction.lang_en: Language.en,
}


def _parse_callback(update: Update) -> TelegramIntent:
    raw = update.callback_data or ""
    action_part, _, id_part = raw.partition(":")
    try:
        action = CallbackAction(action_part)
    except ValueError:
        log.warning("telegram_unknown_callback", callback_data=raw)
        return TelegramIntent(kind=IntentKind.unknown, update_id=update.update_id, text=raw)
    try:
        job_id = int(id_part)
    except ValueError:
        log.warning("telegram_callback_without_job_id", callback_data=raw)
        return TelegramIntent(kind=IntentKind.unknown, update_id=update.update_id, text=raw)
    return TelegramIntent(
        kind=_CALLBACK_INTENT[action],
        update_id=update.update_id,
        job_id=job_id,
        language=_CALLBACK_LANGUAGE.get(action),
    )


def _parse_id_command(update: Update, kind: IntentKind, argument: str) -> TelegramIntent:
    try:
        job_id = int(argument.strip())
    except ValueError:
        return TelegramIntent(kind=IntentKind.unknown, update_id=update.update_id, text=update.text)
    return TelegramIntent(kind=kind, update_id=update.update_id, job_id=job_id)


def _parse_command(update: Update, text: str) -> TelegramIntent:
    command, _, argument = text.partition(" ")
    command = command.lower().lstrip("/")
    argument = argument.strip()
    if command == "pendientes":
        return TelegramIntent(kind=IntentKind.list_pending, update_id=update.update_id)
    if command == "estado":
        return TelegramIntent(kind=IntentKind.status, update_id=update.update_id)
    if command == "buscar":
        return TelegramIntent(
            kind=IntentKind.search, update_id=update.update_id, text=argument or None
        )
    if command == "entrevista":
        return _parse_id_command(update, IntentKind.mark_interview, argument)
    if command == "rechazo":
        return _parse_id_command(update, IntentKind.mark_rejected, argument)
    return TelegramIntent(kind=IntentKind.unknown, update_id=update.update_id, text=text)


def parse_intent(update: Update) -> TelegramIntent:
    """Parse a normalised :class:`Update` into a structured :class:`TelegramIntent`.

    Inline-button callbacks become their action intents; ``/pendientes``,
    ``/estado``, ``/entrevista id``, ``/rechazo id`` and ``/buscar`` become command
    intents; other text becomes ``free_text`` (used for the Ajustar instruction);
    anything unrecognised becomes ``unknown`` and is handled gracefully.
    """
    if update.callback_data:
        return _parse_callback(update)
    text = (update.text or "").strip()
    if not text:
        return TelegramIntent(kind=IntentKind.unknown, update_id=update.update_id)
    if text.startswith("/"):
        return _parse_command(update, text)
    return TelegramIntent(kind=IntentKind.free_text, update_id=update.update_id, text=text)


# --------------------------------------------------------------------------- #
# HTTP transport (injected in tests)
# --------------------------------------------------------------------------- #
class TelegramHttpClient(Protocol):
    """The minimal HTTP surface used to call the Telegram Bot API."""

    def post(self, url: str, **kwargs: Any) -> Any: ...


class _HttpxClient:
    """Default transport backed by ``httpx`` (built lazily so imports stay cheap)."""

    def __init__(self, *, timeout: float = 30.0) -> None:
        self._timeout = timeout

    def post(self, url: str, **kwargs: Any) -> Any:
        import httpx  # noqa: PLC0415 - lazy so importing the module needs no network

        return httpx.post(url, timeout=self._timeout, **kwargs)


# --------------------------------------------------------------------------- #
# The notifier
# --------------------------------------------------------------------------- #
_LANGUAGE_LABELS: dict[Language, str] = {Language.es: "Español", Language.en: "Inglés"}


class TelegramNotifier:
    """Telegram implementation of :class:`~jobpilot.interfaces.Notifier`."""

    def __init__(
        self,
        *,
        bot_token: str,
        owner_chat_id: str,
        http: TelegramHttpClient | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._owner_chat_id = owner_chat_id
        self._http = http or _HttpxClient()

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        http: TelegramHttpClient | None = None,
    ) -> TelegramNotifier:
        """Build from :class:`Settings`, requiring the bot token and owner chat id."""
        settings = settings or get_settings()
        if not settings.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is not configured")
        if not settings.telegram_owner_chat_id:
            raise ValueError("TELEGRAM_OWNER_CHAT_ID is not configured")
        return cls(
            bot_token=settings.telegram_bot_token,
            owner_chat_id=settings.telegram_owner_chat_id,
            http=http,
        )

    # ------------------------------ transport ----------------------------- #
    def _url(self, method: str) -> str:
        return f"{_API_BASE}/bot{self._bot_token}/{method}"

    def _reply_markup(self, buttons: Sequence[Button] | None) -> dict[str, Any] | None:
        if not buttons:
            return None
        return {
            "inline_keyboard": [
                [{"text": b.text, "callback_data": b.callback_data}] for b in buttons
            ]
        }

    def _post(self, method: str, payload: dict[str, Any]) -> None:
        resp = self._http.post(self._url(method), json=payload)
        data = resp.json()
        if not data.get("ok", False):
            log.warning("telegram_api_error", method=method, response=data)
            raise TelegramDeliveryError(f"Telegram {method} failed: {data}")

    def _send_document(self, attachment: Attachment, *, caption: str | None = None) -> None:
        payload: dict[str, Any] = {
            "chat_id": self._owner_chat_id,
            "document": attachment.filename,
        }
        if caption:
            payload["caption"] = caption
        files = {
            "document": (attachment.filename, attachment.content, attachment.mime_type),
        }
        resp = self._http.post(
            self._url("sendDocument"),
            data={k: v for k, v in payload.items() if k != "document"},
            files=files,
        )
        data = resp.json()
        if not data.get("ok", False):
            log.warning("telegram_document_error", filename=attachment.filename, response=data)
            raise TelegramDeliveryError(f"Telegram sendDocument failed: {attachment.filename}")

    # ------------------------------ Notifier ------------------------------ #
    def send_message(
        self,
        text: str,
        buttons: Sequence[Button] | None = None,
        attachments: Sequence[Attachment] | None = None,
    ) -> None:
        """Send a text message to the owner, with optional buttons and documents."""
        for attachment in attachments or ():
            self._send_document(attachment)
        payload: dict[str, Any] = {"chat_id": self._owner_chat_id, "text": text}
        markup = self._reply_markup(buttons)
        if markup is not None:
            payload["reply_markup"] = markup
        self._post("sendMessage", payload)

    def get_updates(self, offset: int | None = None) -> list[Update]:
        """Fetch updates via ``getUpdates`` and drop any chat that is not the owner.

        The ``offset`` acknowledges everything up to ``offset - 1`` on the Telegram
        side; the pipeline passes ``last_update_id + 1`` so processed updates are
        never returned again. Updates from any chat other than the configured owner
        are ignored outright.
        """
        return self.get_update_batch(offset).updates

    def get_update_batch(self, offset: int | None = None) -> UpdateBatch:
        """Fetch a batch, returning owner updates and the highest raw ``update_id``.

        Only the owner's updates are returned in ``updates`` (every other chat is
        refused), but ``highest_update_id`` is computed over *every* update Telegram
        returned, including the ignored non-owner ones. The pipeline checkpoints
        that highest id so a batch of only unauthorized messages still advances the
        cursor and can never starve a later owner update.
        """
        payload: dict[str, Any] = {}
        if offset is not None:
            payload["offset"] = offset
        resp = self._http.post(self._url("getUpdates"), json=payload)
        data = resp.json()
        if not data.get("ok", False):
            log.warning("telegram_get_updates_error", response=data)
            return UpdateBatch(updates=[], highest_update_id=None)
        updates: list[Update] = []
        highest: int | None = None
        for raw in data.get("result", []):
            update = self._normalize_update(raw)
            if update is None:
                continue
            highest = update.update_id if highest is None else max(highest, update.update_id)
            if update.chat_id != self._owner_chat_id:
                log.info(
                    "telegram_ignored_non_owner",
                    update_id=update.update_id,
                    chat_id=update.chat_id,
                )
                continue
            updates.append(update)
        return UpdateBatch(updates=updates, highest_update_id=highest)

    @staticmethod
    def _normalize_update(raw: dict[str, Any]) -> Update | None:
        update_id = raw.get("update_id")
        if update_id is None:
            return None
        callback = raw.get("callback_query")
        if callback is not None:
            message = callback.get("message") or {}
            chat = message.get("chat") or {}
            return Update(
                update_id=int(update_id),
                chat_id=_chat_id(chat),
                callback_data=callback.get("data"),
            )
        message = raw.get("message") or raw.get("edited_message") or {}
        chat = message.get("chat") or {}
        return Update(
            update_id=int(update_id),
            chat_id=_chat_id(chat),
            text=message.get("text"),
        )

    # ------------------------------ proposals ----------------------------- #
    def send_proposal(
        self,
        job: Job,
        *,
        attachments: Sequence[Attachment] = (),
    ) -> None:
        """Send a full proposal for ``job`` with documents and the six-button keyboard."""
        if job.id is None:
            raise ValueError("send_proposal requires a persisted job with an id")
        text = render_proposal_text(job)
        self.send_message(text, buttons=proposal_buttons(job.id), attachments=attachments)

    def ask_language(self, job: Job) -> None:
        """Ask the owner to choose a language for an offer whose language is undecided."""
        if job.id is None:
            raise ValueError("ask_language requires a persisted job with an id")
        self.send_message(render_language_prompt(job), buttons=language_buttons(job.id))


def _chat_id(chat: dict[str, Any]) -> str | None:
    value = chat.get("id")
    return None if value is None else str(value)


# --------------------------------------------------------------------------- #
# Spanish message rendering
# --------------------------------------------------------------------------- #
def render_proposal_text(job: Job) -> str:
    """Render the Spanish proposal message for a scored job."""
    language = _LANGUAGE_LABELS.get(job.language) if job.language else "por decidir"
    score = job.score if job.score is not None else "—"
    lines = [
        "Nueva propuesta de postulación",
        f"Puesto: {job.title}",
        f"Empresa: {job.company}",
        f"Puntaje: {score}",
        f"Idioma: {language}",
    ]
    summary = _proposal_summary(job)
    if summary:
        lines.append(f"Resumen: {summary}")
    if job.missing_requirements:
        lines.append("Requisitos faltantes: " + ", ".join(job.missing_requirements))
    lines.append(f"Oferta: {job.apply_url or job.url}")
    lines.append("")
    lines.append(
        "Elige una acción: Enviar, Ajustar, Descartar, Ya apliqué, "
        "o indica el idioma (Español / Inglés)."
    )
    return "\n".join(lines)


def _proposal_summary(job: Job) -> str | None:
    if job.score_reason:
        return job.score_reason
    if job.analysis is not None and job.analysis.normalized_title:
        return job.analysis.normalized_title
    return None


def render_language_prompt(job: Job) -> str:
    """Render the Spanish ambiguous-language prompt for an offer."""
    return (
        "No pude determinar con seguridad el idioma de esta oferta.\n"
        f"Puesto: {job.title}\n"
        f"Empresa: {job.company}\n"
        f"Oferta: {job.apply_url or job.url}\n\n"
        "¿En qué idioma preparo los documentos? Elige Español o Inglés, "
        "o Descartar para no postularme."
    )
