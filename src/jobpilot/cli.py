"""JobPilot command-line interface (Typer).

Phase 0 wires up the command surface. Subcommands that belong to later phases
print a clear "not implemented yet" message so the CLI is usable end to end.
"""

from __future__ import annotations

from typing import Any

import typer

from jobpilot.logging import configure_logging, get_logger

app = typer.Typer(
    name="jobpilot",
    help="Personal, human-in-the-loop job application bot.",
    no_args_is_help=True,
    add_completion=False,
)

log = get_logger(__name__)


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logs.")) -> None:
    configure_logging("DEBUG" if verbose else "INFO")


@app.command("check-all")
def check_all() -> None:
    """Verify every external credential (Groq, Neon, Telegram, Drive, mail)."""
    from jobpilot.checks import run_all_checks

    ok = run_all_checks()
    raise typer.Exit(code=0 if ok else 1)


@app.command()
def migrate() -> None:
    """Apply pending database migrations (Phase 2)."""
    from jobpilot.config import get_settings
    from jobpilot.storage.migrate import run_migrations
    from jobpilot.storage.neon import connect_with_retry

    settings = get_settings()
    if not settings.database_url:
        typer.echo("Cannot run migrate: DATABASE_URL is not set.", err=True)
        typer.echo("Set DATABASE_URL in your .env and try again.", err=True)
        raise typer.Exit(code=1)

    dsn = settings.database_url

    def conn_factory() -> Any:
        return connect_with_retry(dsn)

    try:
        applied = run_migrations(conn_factory)
    except Exception as exc:  # noqa: BLE001 - surface a clear message, not a traceback
        typer.echo(f"Migration failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if applied:
        typer.echo(f"Applied migrations: {', '.join(applied)}")
    else:
        typer.echo("Database is up to date; no migrations applied.")


@app.command("import-cv")
def import_cv(path: str = typer.Argument(..., help="Path to a PDF or DOCX CV.")) -> None:
    """Import a CV into profile.draft.yaml using the LLM, then print a report."""
    from jobpilot.llm.client import LlmError, build_default_client
    from jobpilot.profile.importer import import_cv as run_import

    try:
        llm = build_default_client()
    except LlmError as exc:
        typer.echo(f"Cannot run import-cv: {exc}", err=True)
        typer.echo("Set GROQ_API_KEY in your .env and try again.", err=True)
        raise typer.Exit(code=1) from exc

    try:
        draft_path, report = run_import(path, llm)
    except (FileNotFoundError, ValueError) as exc:
        typer.echo(f"Import failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"\nDraft written to: {draft_path}\n")
    typer.echo(report.render())


@app.command("auth-mail")
def auth_mail() -> None:
    """Obtain an Outlook/Graph refresh token via the OAuth device-code flow.

    Prints a verification URL and user code, waits for the owner to consent in a
    browser, then prints the refresh token to store as ``GRAPH_REFRESH_TOKEN``.
    """
    from jobpilot.config import get_settings
    from jobpilot.mail.base import MailConfigurationError
    from jobpilot.mail.graph import obtain_refresh_token_device_code

    settings = get_settings()
    if not settings.graph_client_id:
        typer.echo("Cannot run auth-mail: GRAPH_CLIENT_ID is not set.", err=True)
        typer.echo("Set GRAPH_CLIENT_ID in your .env and try again.", err=True)
        raise typer.Exit(code=1)

    try:
        refresh_token = obtain_refresh_token_device_code(
            settings.graph_client_id,
            tenant_id=settings.graph_tenant_id,
            prompt=typer.echo,
        )
    except MailConfigurationError as exc:
        typer.echo(f"auth-mail failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo("\nAuthentication successful. Store this value as GRAPH_REFRESH_TOKEN:\n")
    typer.echo(refresh_token)


@app.command()
def run() -> None:
    """Run the full pipeline once: collect, score, propose and process approvals."""
    from jobpilot.config import PROJECT_ROOT, get_settings, load_app_config
    from jobpilot.pipeline.run import run_once

    settings = get_settings()
    if not settings.database_url:
        typer.echo("Cannot run: DATABASE_URL is not set.", err=True)
        typer.echo("Set DATABASE_URL in your .env and try again.", err=True)
        raise typer.Exit(code=1)

    try:
        config = load_app_config()
        profile = _load_run_profile(PROJECT_ROOT)
        sources, repo, notifier, llm, storage, mailer = _build_run_dependencies(settings, config)
    except (FileNotFoundError, ValueError) as exc:
        typer.echo(f"Cannot run: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    summary = run_once(sources, repo, notifier, profile, llm, storage, config, settings, mailer)
    typer.echo(
        "Run complete: "
        f"{summary.collect.kept_new} new, "
        f"{summary.score.scored} scored, "
        f"{summary.propose.proposed} proposed, "
        f"{summary.telegram.processed} updates processed."
    )


def _load_run_profile(project_root: Any) -> Any:
    from jobpilot.profile.model import load_profile

    return load_profile(project_root / "profile.yaml")


def _build_run_dependencies(settings: Any, config: Any) -> tuple[Any, Any, Any, Any, Any, Any]:
    """Construct the real external dependencies for a run (composition root)."""
    from jobpilot.channels.telegram import TelegramNotifier
    from jobpilot.fetchers.base import iter_enabled
    from jobpilot.llm.client import build_default_client
    from jobpilot.mail.base import build_mailer
    from jobpilot.storage.drive import DriveFileStorage
    from jobpilot.storage.neon import NeonJobRepository

    sources = iter_enabled(config)
    repo = NeonJobRepository.connect(settings.database_url)
    notifier = TelegramNotifier.from_settings(settings)
    llm = build_default_client()
    storage = DriveFileStorage.from_settings(settings)
    mailer = build_mailer(settings)
    return sources, repo, notifier, llm, storage, mailer


if __name__ == "__main__":
    app()
