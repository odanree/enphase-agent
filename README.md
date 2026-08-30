# enphase-agent

## What this is

A home-battery control agent for Enphase systems (IQ Battery behind an IQ Gateway, e.g. inside an X-IQ-AM1-240-6C combiner). It mimics the Enlighten mobile app's **undocumented** REST API to read live state and switch battery mode, reserve SoC, and Storm Guard. This slice is fully deterministic: an adapter, policy guardrails, a rules engine, and a CLI. No LLM anywhere.

Auth and local-endpoint plumbing come from [pyenphase](https://github.com/pyenphase/pyenphase) — the same library behind Home Assistant's `enphase_envoy` integration.

## Legal / ToS warning

Enphase publishes no API for battery control. This tool impersonates the mobile app against `enlighten.enphaseenergy.com` / `entrez.enphaseenergy.com` and the local IQ Gateway. It is **unofficial, unsupported, best-effort**, and can break without notice whenever Enphase changes firmware, auth, or endpoints. Microinverter curtailment is installer-scoped and deliberately not implemented. Use at your own risk and within Enphase's terms of service as you read them.

## Install

```sh
uv sync
```

## Config (env vars)

Copy `.env.example` and fill in:

| Var | Meaning |
|---|---|
| `ENPHASE_EMAIL` | Enlighten account email |
| `ENPHASE_PASSWORD` | Enlighten account password |
| `ENPHASE_GATEWAY_HOST` | LAN IP/host of the IQ Gateway |
| `ENPHASE_SERIAL` | Gateway serial number |

## Usage

```sh
enphase-agent status
enphase-agent set-mode savings --reason "peak tariff"
enphase-agent set-mode backup --confirm --reason "storm inbound"
enphase-agent set-reserve 0.30
enphase-agent plan            # tomorrow's schedule from the rules engine
enphase-agent plan --storm    # what the plan looks like under a storm forecast
```

## Docker

The container ships with its `/data` mount point pre-created and pointed at a **named Docker volume**, not a bind mount. This is deliberate: SQLite uses POSIX advisory locks (`fcntl`), and Docker Desktop's virtiofs (Windows/macOS) / WSL2's 9P do not reliably forward those locks across the host↔container boundary — a bind-mounted SQLite DB gets pseudo-locked the moment anything on the host touches the file. Named volumes stay entirely inside Docker's storage layer and dodge the bug.

```sh
docker compose build
docker compose run --rm enphase-agent status
docker compose run --rm enphase-agent set-mode savings --reason "off-peak"
docker compose run --rm enphase-agent plan
```

The container needs LAN reachability to the Gateway. Default bridge networking works when the Docker host can already route to it.

## Architecture

**Anti-corruption layer** (`adapter.py`). The adapter is the only module that knows Enphase's shapes; callers see our `SystemState` / `BatteryMode`. When Enphase renames a field or pyenphase changes its models, the blast radius is one file.

**Fail-fast at the trust boundary, graceful degradation on the runtime path.** Bad credentials raise `AuthError` on the first call — an auth problem should never be discovered hours into a control loop. But runtime flakiness (gateway offline, cloud hiccup) degrades instead of failing: `get_state` serves the cached snapshot, flags it `stale` once it's older than 10 minutes, and stale state hard-blocks writes via `StaleStateError` — reads may be old, actuation on old data may not.

**Circuit breaker** (asyncio-native, fail_max=5, reset_timeout=300s). The IQ Gateway is a small embedded box; when it's down, pounding it helps nobody. After 5 consecutive failures the breaker opens, calls fail fast for 5 minutes, and reads fall back to the stale cache. Auth errors are excluded — a 401 is a trust-boundary problem, not an availability signal, so it fails fast instead of tripping us open.

**Token-bucket rate limiter.** A 2-second floor between outbound calls, enforced inside the adapter so no caller — including a future scheduler bug — can hammer the gateway.

**Idempotency.** `set_battery_mode` reads current state first and no-ops if the mode already matches. Battery mode writes are expensive (relay/inverter reconfiguration); retries and redundant schedules shouldn't burn them.

**Bulkhead** (`policy.py`). Max 4 mode changes per day, so a runaway caller flaps the policy layer, not the battery. In-memory for now; SQLite persistence is the next PR.

**HITL gate.** `FULL_BACKUP` needs an explicit `confirm=True` (CLI: `--confirm`). It's the one mode that visibly changes household behavior (stops self-consumption, charges from grid), so a human stays in the loop.

**Strategy table / policy-as-data** (`rules.py`). Scheduling logic is a first-match-wins list of `{when, action}` rules — reordering the list is the policy change, and every rule is unit-testable with a plain dict context. `plan_day` runs the table over 24 hours and emits the transitions.

## Tests

```sh
uv run pytest
```

pyenphase is mocked entirely; no hardware or network needed.
