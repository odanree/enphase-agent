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
enphase-agent daemon          # metrics daemon: /metrics on :8000, scrape every 15s
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

## Observability

`docker compose up -d` now brings up three services: the agent in **daemon mode** (a scrape loop exposing Prometheus metrics on `:8000/metrics`), **Prometheus** (`:9090`), and **Grafana** (`:3000`). Browse `http://<host>:3000`, log in as `admin` with the password from `GF_SECURITY_ADMIN_PASSWORD` in `.env` (default `change_me` — change it), and the "Enphase Battery" dashboard is already provisioned.

```sh
docker compose up -d
curl http://localhost:8000/metrics    # raw exporter output
open http://localhost:9090            # Prometheus UI — good for trying PromQL
open http://localhost:3000            # Grafana dashboard
```

One-shot CLI commands still work alongside the daemon: `docker compose run --rm enphase-agent status`.

### Metrics

| Metric | Type | PromQL example | Meaning |
|---|---|---|---|
| `enphase_production_watts` | Gauge | `enphase_production_watts` | Solar production right now |
| `enphase_consumption_watts` | Gauge | `enphase_consumption_watts` | Household consumption right now |
| `enphase_battery_soc_ratio` | Gauge | `enphase_battery_soc_ratio * 100` | Battery state of charge, 0–1 |
| `enphase_battery_reserve_ratio` | Gauge | `enphase_battery_reserve_ratio * 100` | Configured reserve SoC, 0–1 |
| `enphase_battery_mode{mode=...}` | Gauge | `enphase_battery_mode == 1` | Info-style gauge: 1 on the active mode's label, 0 on the others |
| `enphase_storm_guard_active` | Gauge | `enphase_storm_guard_active` | Storm Guard on/off |
| `enphase_state_age_seconds` | Gauge | `enphase_state_age_seconds > 60` | Seconds since the last successful gateway read — the freshness SLI |
| `enphase_scrape_total{outcome=...}` | Counter | `rate(enphase_scrape_total[5m])` | Scrape loop iterations (RED: **R**ate) |
| `enphase_scrape_errors_total{kind=...}` | Counter | `increase(enphase_scrape_errors_total[1h])` | Failures by kind: auth, circuit_open, stale, other (RED: **E**rrors) |
| `enphase_scrape_duration_seconds` | Histogram | `histogram_quantile(0.95, rate(enphase_scrape_duration_seconds_bucket[5m]))` | `get_state` latency (RED: **D**uration) |
| `enphase_circuit_breaker_open` | Gauge | `enphase_circuit_breaker_open == 1` | 1 while the adapter's breaker is failing fast |
| `enphase_writes_total{action,outcome}` | Counter | `sum by (outcome) (increase(enphase_writes_total[24h]))` | Battery write attempts through the policy layer — the audit trail |

### Try these PromQL queries

Each maps to one dashboard panel — paste them into Prometheus at `:9090/graph` to see the raw answer Grafana is charting.

1. **`enphase_production_watts`** — no operator at all. Gauges are instantaneous values, so naming the metric *is* the query; Prometheus returns the latest sample per scrape and Grafana draws the line.
2. **`enphase_battery_soc_ratio * 100`** — scalar arithmetic. The exporter follows the Prometheus convention of publishing ratios as 0–1 (`_ratio` suffix); the `* 100` multiplies every sample by 100 so the gauge panel can render percent. Unit conversions belong in the query layer, not the exporter.
3. **`rate(enphase_scrape_total{outcome="error"}[5m])`** — the counter idiom. Counters only ever go up, so their absolute value is meaningless; `rate(...[5m])` computes the per-second increase averaged over a sliding 5-minute window, turning "total errors ever" into "errors per second right now". (`increase(...[1h])` is the same idea but reports the raw count over the window instead of a per-second rate.)

### Patterns in play

Service metrics follow the **RED method** — Rate (`enphase_scrape_total`), Errors (`enphase_scrape_errors_total`, subclassed by kind), Duration (`enphase_scrape_duration_seconds`) — which answers "is the agent healthy" independently of the **domain gauges** (production, SoC, mode), which answer "is the house healthy". Everything is **pull-based**: the daemon exposes `/metrics` and Prometheus scrapes it, so a dead monitoring stack costs the agent nothing and a dead agent shows up as a down target. **Cardinality discipline**: every label value comes from a closed vocabulary enforced in `metrics.py` — no free-form strings, because one timestamp-in-a-label mistake mints a new time series per scrape. And the stack is **read/write path separated**: Grafana and Prometheus can only ever read — battery writes still flow exclusively through CLI → policy → adapter, so a compromised dashboard can observe your battery but can never drain your reserve.

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
