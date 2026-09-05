from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pyenphase.exceptions import EnvoyAuthenticationError

from enphase_agent.adapter import _MODE_TO_ENPHASE, EnphaseAdapter
from enphase_agent.ledger import Ledger
from enphase_agent.models import BatteryMode


def make_envoy_data(
    *,
    mode: BatteryMode = BatteryMode.SELF_CONSUMPTION,
    soc_pct: int = 55,
    reserve_pct: int = 20,
    storm_guard: bool = False,
    net_consumption_w: int = 300,
) -> SimpleNamespace:
    """Mirror of the pyenphase EnvoyData attributes the adapter reads.

    Energy accumulators mirror EnvoySystemProduction/EnvoySystemConsumption
    (watt_hours_today / _last_7_days / _lifetime + watts_now); net consumption
    shares the consumption shape, positive watts_now = importing from grid.
    """
    return SimpleNamespace(
        system_production=SimpleNamespace(
            watts_now=1500,
            watt_hours_today=12_000,
            watt_hours_last_7_days=90_000,
            watt_hours_lifetime=5_000_000,
        ),
        system_consumption=SimpleNamespace(
            watts_now=800,
            watt_hours_today=9_000,
            watt_hours_last_7_days=70_000,
            watt_hours_lifetime=4_200_000,
        ),
        system_net_consumption=SimpleNamespace(watts_now=net_consumption_w),
        encharge_aggregate=SimpleNamespace(
            state_of_charge=soc_pct,
            available_energy=5_500,
            max_available_capacity=10_080,
        ),
        tariff=SimpleNamespace(
            storage_settings=SimpleNamespace(
                mode=_MODE_TO_ENPHASE[mode],
                reserved_soc=reserve_pct,
                storm_guard=storm_guard,
            )
        ),
    )


class Clock:
    """Injectable clock so staleness and daily-bulkhead tests don't sleep."""

    def __init__(self) -> None:
        self.now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


class FakeEnvoy:
    """Stands in for pyenphase.Envoy — no network, records every call."""

    def __init__(self) -> None:
        self.data = make_envoy_data()
        self.auth_ok = True
        self.update_error: Exception | None = None
        self.calls: list[tuple[Any, ...]] = []

    async def setup(self) -> None:
        self.calls.append(("setup",))

    async def authenticate(self, **kwargs: Any) -> None:
        if not self.auth_ok:
            raise EnvoyAuthenticationError("401: bad credentials")
        self.calls.append(("authenticate",))

    async def update(self) -> Any:
        if self.update_error is not None:
            raise self.update_error
        self.calls.append(("update",))
        return self.data

    async def set_storage_mode(self, mode: Any) -> None:
        self.calls.append(("set_storage_mode", mode))

    async def set_reserve_soc(self, pct: int) -> None:
        self.calls.append(("set_reserve_soc", pct))

    async def set_storm_guard(self, enabled: bool) -> None:
        self.calls.append(("set_storm_guard", enabled))


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def envoy() -> FakeEnvoy:
    return FakeEnvoy()


@pytest.fixture
def data_factory() -> Callable[..., SimpleNamespace]:
    return make_envoy_data


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    # A real file, never ":memory:": WAL needs a filesystem for its -wal/-shm
    # sidecars, and the whole point is exercising real SQLite locking.
    return tmp_path / "enphase.db"


@pytest.fixture
async def ledger(db_path: Path, clock: Clock) -> AsyncIterator[Ledger]:
    async with Ledger(db_path, now_fn=clock) as ledger:
        yield ledger


@pytest.fixture
def adapter(envoy: FakeEnvoy, clock: Clock) -> EnphaseAdapter:
    return EnphaseAdapter(
        host="192.168.1.99",
        email="test@example.com",
        password="hunter2",
        serial="122001001234",
        envoy=envoy,  # type: ignore[arg-type]
        now_fn=clock,
        min_call_interval=0.0,
    )
