"""Mailer abstraction and provider selection.

The pipeline sends an application email through a :class:`Mailer`, which is the
same structural contract declared by :class:`jobpilot.interfaces.Mailer`. Two
concrete providers exist:

* Outlook / Hotmail via Microsoft Graph ``sendMail`` (:mod:`jobpilot.mail.graph`),
  the default (``MAIL_PROVIDER=outlook``);
* Gmail via the Gmail API (:mod:`jobpilot.mail.gmail`), selected with
  ``MAIL_PROVIDER=gmail``.

:func:`build_mailer` is the composition-root factory the CLI uses to construct the
provider configured by ``MAIL_PROVIDER``; tests select the class directly.

SMTP + password (``smtplib``) is intentionally NOT implemented. Both supported
providers (Microsoft and Google) are phasing out basic authentication for personal
mailboxes, and storing a raw mailbox password is a security liability. Only OAuth
(delegated, via a stored refresh token) is supported. The SMTP path is documented
here and deliberately left disabled: do not add an ``smtp`` provider.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from jobpilot.config import MailProvider, Settings, get_settings
from jobpilot.models import Attachment


class MailConfigurationError(RuntimeError):
    """Raised when the selected provider is missing a required credential."""


class Mailer(ABC):
    """Outbound email with attachments (structural match for the Notifier contract).

    Concrete providers authenticate with OAuth (a stored refresh token) and send a
    message that lands in the mailbox's Sent folder, so the owner keeps a record of
    every application sent on their behalf.
    """

    @abstractmethod
    def send(
        self,
        to: str,
        subject: str,
        body: str,
        attachments: Sequence[Attachment],
    ) -> None:
        """Send an email to ``to`` with ``subject``/``body`` and ``attachments``."""


def build_mailer(settings: Settings | None = None) -> Mailer:
    """Build the :class:`Mailer` selected by ``MAIL_PROVIDER`` (outlook default).

    ``outlook`` builds the Microsoft Graph mailer; ``gmail`` builds the Gmail
    mailer. Both read their OAuth credentials from :class:`Settings`. Raises
    :class:`MailConfigurationError` when a required credential is absent, so the
    composition root fails with a clear message instead of at send time.
    """
    settings = settings or get_settings()
    if settings.mail_provider is MailProvider.gmail:
        from jobpilot.mail.gmail import GmailMailer  # noqa: PLC0415 - avoid import cycle

        return GmailMailer.from_settings(settings)

    from jobpilot.mail.graph import GraphMailer  # noqa: PLC0415 - avoid import cycle

    return GraphMailer.from_settings(settings)
