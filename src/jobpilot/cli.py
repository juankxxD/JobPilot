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

_NOT_IMPLEMENTED = "This command is implemented in a later phase."


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
    """Run the one-time OAuth consent flow for the mail provider (Phase 6)."""
    typer.echo(f"auth-mail: {_NOT_IMPLEMENTED}")


@app.command()
def run() -> None:
    """Run the full pipeline once (Phase 5+)."""
    typer.echo(f"run: {_NOT_IMPLEMENTED}")


if __name__ == "__main__":
    app()
