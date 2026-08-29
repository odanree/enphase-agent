"""Typer CLI — thin shell over adapter/policy/rules; no business logic here."""

from __future__ import annotations

import asyncio
import os
from enum import Enum
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from .adapter import EnphaseAdapter
from .errors import EnphaseAgentError
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


def _run(coro: Any) -> Any:
    try:
        return asyncio.run(coro)
    except EnphaseAgentError as exc:
        console.print(f"[red]{type(exc).__name__}: {exc}[/red]")
        raise typer.Exit(1) from exc


@app.command()
def status() -> None:
    """Print the current system state."""
    state: SystemState = _run(_adapter().get_state())
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
    # The mode-change bulkhead is in-memory, so one CLI invocation = one
    # counter; it protects long-lived callers today. SQLite is the next PR.
    policy = BatteryPolicy(_adapter())
    _run(policy.set_battery_mode(_MODE_ARG_MAP[mode], reason=reason, confirm=confirm))
    console.print(f"[green]Mode set to {mode.value}[/green]")


@app.command("set-reserve")
def set_reserve(
    pct: float = typer.Argument(..., help="Reserve SoC as a fraction, e.g. 0.30."),
    reason: str = typer.Option("cli", help="Audit-trail reason recorded with the write."),
) -> None:
    """Set the battery reserve SoC through the policy guardrails."""
    policy = BatteryPolicy(_adapter())
    _run(policy.set_reserve_soc(pct, reason=reason))
    console.print(f"[green]Reserve set to {pct:.0%}[/green]")


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
