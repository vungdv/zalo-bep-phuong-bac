"""Command-line entrypoint and composition root (design §10).

The commands are stubs until task T15 wires them to the orchestrator and repositories.
"""

from __future__ import annotations

from typing import Annotated

import typer

app = typer.Typer(
    name="order-extractor",
    help="Read orders from the open Zalo Web chat and append them to Google Sheets.",
    no_args_is_help=True,
    add_completion=False,
)

NOT_IMPLEMENTED_EXIT = 2


def _not_implemented(command: str) -> None:
    typer.echo(f"{command}: not implemented yet", err=True)
    raise typer.Exit(code=NOT_IMPLEMENTED_EXIT)


@app.command()
def start(
    since: Annotated[
        str | None,
        typer.Option(help="Start watermark for conversations first seen in this run (ISO time)."),
    ] = None,
) -> None:
    """Open Zalo Web and keep reading the open conversation."""
    _not_implemented("start")


@app.command()
def status() -> None:
    """Print order counts per status and the oldest NEW/FAILED order."""
    _not_implemented("status")


@app.command()
def reprocess(
    order: Annotated[int | None, typer.Option(help="Reset a FAILED order to NEW.")] = None,
    conversation: Annotated[str | None, typer.Option(help="Conversation key to re-check.")] = None,
    since: Annotated[str | None, typer.Option(help="Re-check messages sent at or after this time.")] = None,
) -> None:
    """Queue a FAILED order or a range of messages for another pass on the next start."""
    _not_implemented("reprocess")
