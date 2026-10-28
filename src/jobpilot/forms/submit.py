"""Human-in-the-loop pre-submit confirmation for assisted forms (LOCAL only).

A filled form is NEVER submitted automatically. Before the final click, the owner
receives a screenshot of the filled form plus the list of uncertain fields and any
unanswered sensitive questions, with two buttons: Enviar (submit) and Cancelar
(do not submit). Only an explicit Enviar performs the final click and records the
application; Cancelar (or a filler abort) leaves the form untouched and submits
nothing.

The confirmation is synchronous (this is a local run): the prompt is sent, then the
owner's decision is read back through the same :class:`~jobpilot.interfaces.Notifier`
used everywhere else, by polling ``get_update_batch`` and matching the Enviar/Cancelar
callback for this job. The notifier (and the clock/sleep) are injected so tests drive
the whole flow without Telegram or real waiting.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from jobpilot.interfaces import Button
from jobpilot.logging import get_logger
from jobpilot.models import Attachment

if TYPE_CHECKING:
    from jobpilot.interfaces import FillResult, Notifier

log = get_logger(__name__)


class SubmitAction(StrEnum):
    """The owner's decision on a filled form, encoded as ``action:job_id``."""

    submit = "form_submit"
    cancel = "form_cancel"


class SubmitDecision(StrEnum):
    """The outcome of the pre-submit confirmation."""

    submit = "submit"
    cancel = "cancel"
    timeout = "timeout"


def submit_buttons(job_id: int) -> list[Button]:
    """The Enviar / Cancelar inline buttons shown before a form submit."""
    return [
        Button(text="Enviar", callback_data=f"{SubmitAction.submit.value}:{job_id}"),
        Button(text="Cancelar", callback_data=f"{SubmitAction.cancel.value}:{job_id}"),
    ]


def render_presubmit_text(job_id: int, result: FillResult) -> str:
    """Render the Spanish pre-submit summary for the owner."""
    lines = [
        "Formulario listo para revisar antes de enviar.",
        f"Oferta: {job_id}",
        f"Campos completados: {len(result.filled_fields)}",
    ]
    if result.uncertain_fields:
        lines.append("Campos inciertos (revísalos): " + ", ".join(result.uncertain_fields))
    else:
        lines.append("No hay campos inciertos.")
    if result.unanswered_sensitive:
        lines.append(
            "Preguntas sensibles sin responder (déjalas o complétalas tú): "
            + ", ".join(result.unanswered_sensitive)
        )
    lines.append("")
    lines.append('Revisa la captura y pulsa "Enviar" para postular o "Cancelar" para no enviar.')
    return "\n".join(lines)


def screenshot_attachment(screenshot_path: str | None) -> list[Attachment]:
    """Load the screenshot (if any) as a PNG attachment for the Telegram preview."""
    if not screenshot_path:
        return []
    path = Path(screenshot_path)
    if not path.exists():
        log.warning("form_screenshot_missing", path=screenshot_path)
        return []
    return [Attachment(filename=path.name, content=path.read_bytes(), mime_type="image/png")]


def request_submit_confirmation(
    notifier: Notifier,
    job_id: int,
    result: FillResult,
    *,
    poll_attempts: int = 60,
    poll_interval_s: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> SubmitDecision:
    """Ask the owner to confirm a form submit and return their decision.

    Sends the screenshot + the uncertain/sensitive-field summary with Enviar/Cancelar
    buttons, then polls ``get_update_batch`` for the matching callback. Returns
    :attr:`SubmitDecision.submit` only on an explicit Enviar for THIS job,
    :attr:`SubmitDecision.cancel` on Cancelar, and :attr:`SubmitDecision.timeout` if
    no decision arrives within ``poll_attempts`` polls. A timeout is treated by the
    caller exactly like a cancel: nothing is submitted.
    """
    attachments = screenshot_attachment(result.screenshot_path)
    notifier.send_message(
        render_presubmit_text(job_id, result),
        buttons=submit_buttons(job_id),
        attachments=attachments,
    )
    submit_cb = f"{SubmitAction.submit.value}:{job_id}"
    cancel_cb = f"{SubmitAction.cancel.value}:{job_id}"
    offset: int | None = None
    for _ in range(poll_attempts):
        batch = notifier.get_update_batch(offset=offset)
        for update in sorted(batch.updates, key=lambda u: u.update_id):
            offset = update.update_id + 1
            if update.callback_data == submit_cb:
                log.info("form_submit_confirmed", job_id=job_id)
                return SubmitDecision.submit
            if update.callback_data == cancel_cb:
                log.info("form_submit_cancelled", job_id=job_id)
                return SubmitDecision.cancel
        if batch.highest_update_id is not None:
            offset = batch.highest_update_id + 1
        sleep(poll_interval_s)
    log.warning("form_submit_timeout", job_id=job_id)
    return SubmitDecision.timeout
