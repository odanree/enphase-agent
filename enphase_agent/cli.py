"""Typer CLI — thin shell over adapter/policy/rules; no business logic here."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from enum import Enum
from pathlib import Path
from typing import Any

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

load_dotenv()

from .adapter import EnphaseAdapter
from .daemon import DEFAULT_INTERVAL_S, DEFAULT_PORT, run_daemon
from .errors import EnphaseAgentError
from .ledger import Ledger, WriteRow
from .models import BatteryMode, SystemState
from .policy import BatteryPolicy
from .rules import plan_day

app = typer.Typer(no_args_is_help=True, help="Unofficial Enphase battery control agent.")
console = Console()

_REQUIRED_ENV = (
    "ENPHASE_EMAIL",
    "ENPHASE_PASSWORD",
    "ENPHASE_GATEWAY_HOST",
    "ENPHASE_SERIAL",
)

# Local-dev fallback; compose and the Dockerfile both pin /data/enphase.db.
_DEFAULT_DB_PATH = Path("enphase.db")

_OUTCOME_STYLE = {"success": "green", "rejected": "yellow", "error": "red"}


class ModeArg(str, Enum):
    SELF_CONSUMPTION = "self-consumption"
    SAVINGS = "savings"
    BACKUP = "backup"


_MODE_ARG_MAP = {
    ModeArg.SELF_CONSUMPTION: BatteryMode.SELF_CONSUMPTION,
    ModeArg.SAVINGS: BatteryMode.SAVINGS,
    ModeArg.BACKUP: BatteryMode.FULL_BACKUP,
}


def _adapter() -> EnphaseAdapter:
    missing = [var for var in _REQUIRED_ENV if not os.getenv(var)]
    if missing:
        console.print(f"[red]Missing env vars: {', '.join(missing)}[/red]")
        raise typer.Exit(2)
    return EnphaseAdapter(
        host=os.environ["ENPHASE_GATEWAY_HOST"],
        email=os.environ["ENPHASE_EMAIL"],
        password=os.environ["ENPHASE_PASSWORD"],
        serial=os.environ["ENPHASE_SERIAL"],
    )


def _db_path() -> Path:
    # `or`, not a getenv default: compose/env_file hand over "" for an unset
    # var, and "" must fall through to the default like an absent one.
    return Path(os.getenv("ENPHASE_DB_PATH") or _DEFAULT_DB_PATH)


async def _with_adapter(work: Callable[[EnphaseAdapter], Awaitable[Any]]) -> Any:
    async with _adapter() as adapter:
        return await work(adapter)


async def _with_adapter_and_ledger(
    work: Callable[[EnphaseAdapter, Ledger], Awaitable[Any]],
) -> Any:
    # Ledger first: it's the cheap local check, and a write command that
    # can't audit itself should fail before it touches the gateway.
    async with Ledger(_db_path()) as ledger, _adapter() as adapter:
        return await work(adapter, ledger)


def _report(exc: EnphaseAgentError) -> typer.Exit:
    console.print(f"[red]{type(exc).__name__}: {exc}[/red]")
    return typer.Exit(1)


def _run(work: Callable[[EnphaseAdapter], Awaitable[Any]]) -> Any:
    try:
        return asyncio.run(_with_adapter(work))
    except EnphaseAgentError as exc:
        raise _report(exc) from exc


def _run_with_ledger(work: Callable[[EnphaseAdapter, Ledger], Awaitable[Any]]) -> Any:
    try:
        return asyncio.run(_with_adapter_and_ledger(work))
    except EnphaseAgentError as exc:
        raise _report(exc) from exc


@app.command()
def status() -> None:
    """Print the current system state."""
    state: SystemState = _run(lambda a: a.get_state())
    table = Table(title="Enphase system state")
    table.add_column("Field")
    table.add_column("Value", justify="right")
    table.add_row("Production", f"{state.production_w:,.0f} W")
    table.add_row("Consumption", f"{state.consumption_w:,.0f} W")
    table.add_row("Battery SoC", f"{state.battery_soc:.0%}")
    table.add_row("Mode", state.battery_mode.name)
    table.add_row("Reserve SoC", f"{state.reserve_soc:.0%}")
    table.add_row("Storm guard", "on" if state.storm_guard else "off")
    table.add_row("As of", state.ts.isoformat(timespec="seconds"))
    table.add_row("Stale", "[red]YES[/red]" if state.stale else "no")
    console.print(table)


@app.command("set-mode")
def set_mode(
    mode: ModeArg = typer.Argument(..., help="Target battery mode."),
    reason: str = typer.Option("cli", help="Audit-trail reason recorded with the write."),
    confirm: bool = typer.Option(
        False, "--confirm", help="Required for backup (human-in-the-loop gate)."
    ),
) -> None:
    """Change battery mode through the policy guardrails."""
    _run_with_ledger(
        lambda a, led: BatteryPolicy(a, ledger=led).set_battery_mode(
            _MODE_ARG_MAP[mode], reason=reason, confirm=confirm
        )
    )
    console.print(f"[green]Mode set to {mode.value}[/green]")


@app.command("set-reserve")
def set_reserve(
    pct: float = typer.Argument(..., help="Reserve SoC as a fraction, e.g. 0.30."),
    reason: str = typer.Option("cli", help="Audit-trail reason recorded with the write."),
) -> None:
    """Set the battery reserve SoC through the policy guardrails."""
    _run_with_ledger(
        lambda a, led: BatteryPolicy(a, ledger=led).set_reserve_soc(pct, reason=reason)
    )
    console.print(f"[green]Reserve set to {pct:.0%}[/green]")


@app.command("ledger")
def ledger_cmd(
    limit: int = typer.Option(20, help="Number of most recent write attempts to show."),
) -> None:
    """Dump the audit ledger (newest first). Read-only; needs no gateway."""

    async def _read() -> list[WriteRow]:
        async with Ledger(_db_path()) as ledger:
            return await ledger.recent(limit)

    try:
        rows = asyncio.run(_read())
    except EnphaseAgentError as exc:
        raise _report(exc) from exc

    table = Table(title=f"Audit ledger — last {len(rows)} of up to {limit} ({_db_path()})")
    for column in ("ts", "action", "outcome", "target", "reason", "error_class"):
        table.add_column(column)
    for row in rows:
        style = _OUTCOME_STYLE.get(row.outcome, "")
        table.add_row(
            row.ts,
            row.action,
            f"[{style}]{row.outcome}[/{style}]" if style else row.outcome,
            row.target or "",
            row.reason or "",
            row.error_class or "",
        )
    console.print(table)


@app.command()
def daemon(
    interval: float = typer.Option(
        DEFAULT_INTERVAL_S, help="Seconds between gateway scrapes."
    ),
    port: int = typer.Option(DEFAULT_PORT, help="Port for the Prometheus /metrics endpoint."),
) -> None:
    """Run the pull-based metrics daemon (Prometheus scrapes us; we never push)."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        # Ledger open failure propagates as LedgerError → exit 1 before
        # /metrics is served: fail-fast at the trust boundary.
        _run_with_ledger(lambda a, led: run_daemon(a, interval_s=interval, port=port, ledger=led))
    except KeyboardInterrupt:
        # Windows dev host: no loop signal handlers, Ctrl+C lands here.
        # run_daemon's finally already closed the adapter.
        raise typer.Exit(0) from None


@app.command()
def plan(
    soc: float = typer.Option(0.5, help="Assumed current SoC (fraction)."),
    storm: bool = typer.Option(False, "--storm", help="Assume a storm forecast."),
) -> None:
    """Print tomorrow's deterministic schedule from the rules engine (no LLM)."""
    schedule = plan_day({"soc": soc, "storm_forecast": storm})
    table = Table(title="Next 24h plan (first-match-wins rules)")
    table.add_column("From")
    table.add_column("Rule")
    table.add_column("Action")
    for item in schedule:
        table.add_row(f"{item.hour:02d}:00", item.rule, item.description)
    console.print(table)


if __name__ == "__main__":
    app()
