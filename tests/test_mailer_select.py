"""Provider selection: MAIL_PROVIDER picks the right Mailer (constructors faked)."""

from __future__ import annotations

import pytest

from jobpilot.config import MailProvider, Settings
from jobpilot.mail.base import Mailer, build_mailer
from jobpilot.mail.gmail import GmailMailer
from jobpilot.mail.graph import GraphMailer


def _outlook_settings() -> Settings:
    return Settings(
        mail_provider=MailProvider.outlook,
        graph_client_id="client-123",
        graph_refresh_token="refresh-abc",
        mail_from_address="me@example.com",
    )


def _gmail_settings() -> Settings:
    return Settings(
        mail_provider=MailProvider.gmail,
        gmail_client_id="gmail-client",
        gmail_client_secret="gmail-secret",
        gmail_refresh_token="gmail-refresh",
        mail_from_address="me@example.com",
    )


def test_default_provider_is_outlook() -> None:
    assert Settings().mail_provider is MailProvider.outlook


def test_build_mailer_selects_graph_for_outlook() -> None:
    mailer = build_mailer(_outlook_settings())
    assert isinstance(mailer, GraphMailer)
    assert isinstance(mailer, Mailer)


def test_build_mailer_selects_gmail_for_gmail() -> None:
    # Gmail's client is built lazily (on first send), so no Google call happens here.
    mailer = build_mailer(_gmail_settings())
    assert isinstance(mailer, GmailMailer)
    assert isinstance(mailer, Mailer)


def test_build_mailer_outlook_requires_client_id() -> None:
    from jobpilot.mail.base import MailConfigurationError

    with pytest.raises(MailConfigurationError, match="GRAPH_CLIENT_ID"):
        build_mailer(Settings(mail_provider=MailProvider.outlook, graph_client_id=None))


def test_build_mailer_outlook_requires_refresh_token() -> None:
    from jobpilot.mail.base import MailConfigurationError

    with pytest.raises(MailConfigurationError, match="GRAPH_REFRESH_TOKEN"):
        build_mailer(
            Settings(
                mail_provider=MailProvider.outlook,
                graph_client_id="client-123",
                graph_refresh_token=None,
            )
        )
