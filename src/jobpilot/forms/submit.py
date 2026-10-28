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

import secrets
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
    """The owner's decision on a filled form, encoded as ``action:job_id:nonce``."""

    submit = "form_submit"
    cancel = "form_cancel"


def new_confirmation_nonce() -> str:
    """Return a short, unguessable nonce identifying one confirmation attempt.

    Each pre-submit prompt gets a fresh nonce bound into both buttons' callback
    data, so a delayed Enviar/Cancelar from an earlier (timed-out or crashed)
    attempt for the same job carries a different nonce and is ignored. Only the
    current attempt's exact token can authorize the final click.
    """
    return secrets.token_hex(8)


class SubmitDecision(StrEnum):
    """The outcome of the pre-submit confirmation."""

    submit = "submit"
    cancel = "cancel"
    timeout = "timeout"


def submit_buttons(job_id: int, nonce: str) -> list[Button]:
    """The Enviar / Cancelar inline buttons for one confirmation attempt.

    Both callbacks embed ``nonce`` so only this attempt's buttons are honoured; a
    stale callback from a prior attempt for the same job carries a different nonce.
    """
    return [
        Button(text="Enviar", callback_data=f"{SubmitAction.submit.value}:{job_id}:{nonce}"),
        Button(text="Cancelar", callback_data=f"{SubmitAction.cancel.value}:{job_id}:{nonce}"),
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
    nonce: str | None = None,
) -> SubmitDecision:
    """Ask the owner to confirm a form submit and return their decision.

    A fresh confirmation nonce is bound into the Enviar/Cancelar callbacks, and any
    Telegram backlog queued *before* the prompt is drained first (the cursor starts
    past the highest pending update), so a delayed Enviar from an earlier attempt for
    the same job — which carries a different nonce and predates this prompt — can
    never authorize this submission. Only a callback that both arrives after the
    prompt and matches this attempt's exact nonce counts.

    Returns :attr:`SubmitDecision.submit` only on this attempt's explicit Enviar,
    :attr:`SubmitDecision.cancel` on its Cancelar, and :attr:`SubmitDecision.timeout`
    if no matching decision arrives within ``poll_attempts`` polls. A timeout is
    treated by the caller exactly like a cancel: nothing is submitted.
    """
    attempt_nonce = nonce or new_confirmation_nonce()

    # Drain any backlog so a stale callback queued before this prompt is skipped: the
    # cursor begins just past the highest update Telegram currently holds.
    offset: int | None = None
    backlog = notifier.get_update_batch(offset=None)
    if backlog.highest_update_id is not None:
        offset = backlog.highest_update_id + 1

    attachments = screenshot_attachment(result.screenshot_path)
    notifier.send_message(
        render_presubmit_text(job_id, result),
        buttons=submit_buttons(job_id, attempt_nonce),
        attachments=attachments,
    )
    submit_cb = f"{SubmitAction.submit.value}:{job_id}:{attempt_nonce}"
    cancel_cb = f"{SubmitAction.cancel.value}:{job_id}:{attempt_nonce}"
    for _ in range(poll_attempts):
        batch = notifier.get_update_batch(offset=offset)
        for update in sorted(batch.updates, key=lambda u: u.update_id):
            offset = update.update_id + 1
            if update.callback_data == submit_cb:
                log.info("form_submit_confirmed", job_id=job_id, nonce=attempt_nonce)
                return SubmitDecision.submit
            if update.callback_data == cancel_cb:
                log.info("form_submit_cancelled", job_id=job_id, nonce=attempt_nonce)
                return SubmitDecision.cancel
            if _is_stale_same_job_callback(update.callback_data, job_id, attempt_nonce):
                log.info(
                    "form_submit_stale_callback_ignored",
                    job_id=job_id,
                    callback=update.callback_data,
                )
        if batch.highest_update_id is not None:
            offset = batch.highest_update_id + 1
        sleep(poll_interval_s)
    log.warning("form_submit_timeout", job_id=job_id)
    return SubmitDecision.timeout


def _is_stale_same_job_callback(callback: str | None, job_id: int, nonce: str) -> bool:
    """Return whether ``callback`` is a submit/cancel for this job but a different nonce."""
    if callback is None:
        return False
    for action in (SubmitAction.submit.value, SubmitAction.cancel.value):
        prefix = f"{action}:{job_id}:"
        if callback.startswith(prefix) and callback != f"{prefix}{nonce}":
            return True
    return False
