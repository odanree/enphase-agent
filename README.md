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
| `ENPHASE_DB_PATH` | SQLite audit ledger path. Compose sets `/data/enphase.db`; unset locally → `./enphase.db` |

## Usage

```sh
enphase-agent status
enphase-agent set-mode savings --reason "peak tariff"
enphase-agent set-mode backup --confirm --reason "storm inbound"
enphase-agent set-reserve 0.30
enphase-agent plan            # tomorrow's schedule from the rules engine
enphase-agent plan --storm    # what the plan looks like under a storm forecast
enphase-agent daemon          # metrics daemon: /metrics on :8000, scrape every 15s
enphase-agent ledger          # last 20 write attempts from the audit ledger
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
| `enphase_writes_total{action,outcome}` | Counter | `sum by (outcome) (increase(enphase_writes_total[24h]))` | Battery write attempts made **by this process**. The daemon never writes, so on the daemon this stays 0 — see the ledger gauge below |
| `enphase_writes_last_24h{action,outcome}` | Gauge | `enphase_writes_last_24h{outcome="rejected"}` | Write attempts in the trailing 24h **across every process**, read from the audit ledger each scrape (materialized view) |
| `enphase_energy_produced_today_watt_hours` | Gauge | `enphase_energy_produced_today_watt_hours / 1000` | Solar energy since local midnight (gateway accumulator; resets daily — that reset is why it's a Gauge, not a Counter) |
| `enphase_energy_produced_7d_watt_hours` | Gauge | `enphase_energy_produced_7d_watt_hours / 1000` | Solar energy over the previous 7 days, excluding today (rolling window) |
| `enphase_energy_produced_lifetime_watt_hours` | Gauge | `rate(enphase_energy_produced_lifetime_watt_hours[1h])` | Lifetime solar energy. Monotonic at the source but exported as a Gauge — we mirror the value rather than own the increments, and `_total` on a Gauge would mislead; `rate()` works on it regardless |
| `enphase_energy_consumed_today_watt_hours` | Gauge | `enphase_energy_consumed_today_watt_hours / 1000` | Household energy since local midnight (CT-metered gateways only) |
| `enphase_energy_consumed_7d_watt_hours` | Gauge | `enphase_energy_consumed_7d_watt_hours / 1000` | Household energy over the previous 7 days, excluding today |
| `enphase_energy_consumed_lifetime_watt_hours` | Gauge | `rate(enphase_energy_consumed_lifetime_watt_hours[1h])` | Lifetime household energy consumed |
| `enphase_battery_energy_available_watt_hours` | Gauge | `enphase_battery_energy_available_watt_hours / 1000` | Usable energy in the battery right now |
| `enphase_battery_energy_capacity_watt_hours` | Gauge | `enphase_battery_energy_available_watt_hours / enphase_battery_energy_capacity_watt_hours` | Battery nameplate capacity (divide available by it for a Wh-based SoC cross-check) |
| `enphase_grid_import_watts` | Gauge | `enphase_grid_import_watts > 0` | Power drawn from the grid right now (always ≥ 0) |
| `enphase_grid_export_watts` | Gauge | `enphase_grid_export_watts > 0` | Power pushed to the grid right now (always ≥ 0) |

The grid pair is the **directional gauges over signed values** pattern: the gateway reports one signed net-consumption power, but signed metrics are hard to stack, hard to color-threshold, and confuse `rate()` downstream — so the exporter splits it into two always-non-negative gauges, at most one nonzero at a time.

All of the energy/grid metrics come from optional gateway sources (CT metering, Encharge aggregate). When the source is absent the exporter publishes **nothing** — an absent series you can catch with `absent(...)` — rather than a fake `0` that would poison daily-total panels.

### Try these PromQL queries

Each maps to one dashboard panel — paste them into Prometheus at `:9090/graph` to see the raw answer Grafana is charting.

1. **`enphase_production_watts`** — no operator at all. Gauges are instantaneous values, so naming the metric *is* the query; Prometheus returns the latest sample per scrape and Grafana draws the line.
2. **`enphase_battery_soc_ratio * 100`** — scalar arithmetic. The exporter follows the Prometheus convention of publishing ratios as 0–1 (`_ratio` suffix); the `* 100` multiplies every sample by 100 so the gauge panel can render percent. Unit conversions belong in the query layer, not the exporter.
3. **`rate(enphase_scrape_total{outcome="error"}[5m])`** — the counter idiom. Counters only ever go up, so their absolute value is meaningless; `rate(...[5m])` computes the per-second increase averaged over a sliding 5-minute window, turning "total errors ever" into "errors per second right now". (`increase(...[1h])` is the same idea but reports the raw count over the window instead of a per-second rate.)
4. **`enphase_energy_produced_today_watt_hours / 1000`** — the "Produced today" stat. The gateway already accumulates since-midnight energy, so the query is just a unit conversion to kWh; no `sum_over_time` gymnastics needed when the source does the integration for you.
5. **`enphase:solar_coverage_24h:ratio * 100`** — the "Solar coverage" stat. Note the colons: this is a recording rule, not a raw metric. The rule computes `1 - (grid-import energy / consumption energy)` over the trailing 24h once a minute, so the panel reads one precomputed sample instead of running a 24h subquery per refresh.
6. **`enphase_grid_import_watts` / `enphase_grid_export_watts`** — the "Grid flow now" stat. Two directional gauges split from one signed reading; whichever is nonzero tells you which way electrons are flowing.
7. **`enphase:time_in_mode_24h:seconds`** — the mode pie. `sum_over_time(enphase_battery_mode[24h:15s]) * 15` integrates the one-hot mode gauge: each 15s sample where a mode was active contributes 15 seconds to its slice — an info-gauge integrated over time, i.e. a Riemann sum in PromQL.

### Recording rules

`prometheus/rules.yml` holds derived series — **materialized views for the observability stack**. A recording rule precomputes an expensive expression on a fixed cadence (ours run every 1m) and stores the result as a new series, so dashboards read one sample instead of re-running a 24h subquery on every refresh. Same tradeoff as a database materialized view: storage and refresh lag bought back as query latency.

The naming convention is `namespace:metric_name:aggregation_unit` (e.g. `enphase:solar_coverage_24h:ratio`). Raw metric names never contain `:`, so the colons are the standard Prometheus tell that a series is derived, not scraped.

When to add one: when a panel's query cost exceeds its refresh interval — a 24h subquery at 15s resolution re-evaluated every 30s by every open browser tab is the textbook case. Instant gauges (`enphase_production_watts`) never need one.

Iterating on rules doesn't require a container restart — the compose file starts Prometheus with `--web.enable-lifecycle`, so after editing `rules.yml`:

```sh
curl -X POST http://localhost:9090/-/reload
```

### Retention

Prometheus keeps **1 year** of raw 15s samples (`--storage.tsdb.retention.time=1y` in compose, up from the 15d default) — the **hot-storage-only long retention** pattern: no downsampling, every sample queryable at full resolution. The disk math makes it a non-decision at this scale: Prometheus averages ~1–2 bytes per sample post-compression, and this exporter emits ~40 series × 4 samples/min ≈ 84M samples/year — on the order of **1 GB/year**. Trivial for a home tool.

The pattern stops scaling when cardinality does. If this stack ever grew to thousands of series (per-inverter metrics, per-circuit CTs), the graduation path is **downsampled cold storage**: a Thanos or Mimir sidecar (or Grafana Cloud) that keeps recent data raw and rolls older data up to 5m/1h resolution. The recording rules above are the miniature, manual version of the same idea — precompute the aggregates you'll actually query.

### Patterns in play

Service metrics follow the **RED method** — Rate (`enphase_scrape_total`), Errors (`enphase_scrape_errors_total`, subclassed by kind), Duration (`enphase_scrape_duration_seconds`) — which answers "is the agent healthy" independently of the **domain gauges** (production, SoC, mode), which answer "is the house healthy". Everything is **pull-based**: the daemon exposes `/metrics` and Prometheus scrapes it, so a dead monitoring stack costs the agent nothing and a dead agent shows up as a down target. **Cardinality discipline**: every label value comes from a closed vocabulary enforced in `metrics.py` — no free-form strings, because one timestamp-in-a-label mistake mints a new time series per scrape. And the stack is **read/write path separated**: Grafana and Prometheus can only ever read — battery writes still flow exclusively through CLI → policy → adapter, so a compromised dashboard can observe your battery but can never drain your reserve.

## Audit ledger

Every battery write attempt — allowed, rejected, or failed — is appended to a SQLite database at `ENPHASE_DB_PATH` (`/data/enphase.db` in the container, on the `enphase_data` named volume). One table, `writes`, in WAL mode:

| Column | Meaning |
|---|---|
| `ts` | UTC ISO 8601 (`2026-09-04T22:15:03.421Z`) |
| `action` | `set_mode` / `set_reserve` / `storm_guard` |
| `outcome` | `success` / `rejected` / `error` |
| `target` | new mode name, reserve fraction, or bool as string |
| `reason` | the caller's `--reason`, plus the rejection message when rejected |
| `error_class` | exception class name when `outcome=error` |

### Why it exists

1. **The CLI↔daemon audit gap.** `enphase_writes_total` is a per-process Prometheus counter, but writes happen in one-shot CLI processes (`docker compose run --rm enphase-agent set-mode …`) while Prometheus scrapes the long-running daemon. The scraped process never performs a write, so its counter was permanently 0. The ledger is the durable state both share: CLI processes append, the daemon reads the trailing 24h back every scrape and exports it as `enphase_writes_last_24h{action,outcome}`.
2. **Bulkhead persistence.** The 4-mode-changes-per-day guard used to be a Python list — which meant every CLI invocation (a fresh process) started at zero, and the bulkhead only ever guarded a single process's lifetime. It is now a query over today's committed rows, so it holds across CLI invocations and container restarts.
3. **A substrate for the planner.** A future LLM planner can read "what did I do recently, and why" from one table instead of reconstructing it from logs.

### Patterns in play

**WAL for concurrent-reader-single-writer semantics.** `PRAGMA journal_mode=WAL` lets the daemon read while a CLI process commits — readers see the last committed snapshot and never block on the writer. Two CLI processes racing serialize on SQLite's single write lock for the milliseconds a commit takes (`timeout` on the connection makes that a short wait, not a "database is locked" error). This is the classic shared-state contention point and it's fine here because the bulkhead caps writes at a handful per day. WAL's `-shm` index depends on fcntl locks, which is exactly why `/data` is a named volume and not a bind mount.

**Audit trail as a materialized view.** The daemon doesn't own write counts; it re-derives `enphase_writes_last_24h` from the ledger every iteration — same shape as the Prometheus recording rules above, one layer lower.

**Write-through instrumentation.** `BatteryPolicy` records each attempt from one call site that fans out to both sinks (metrics counter + ledger row), so the two can never disagree about what was attempted.

**Bulkhead persistence, with asymmetric failure handling.** The bulkhead *check* (ledger read) fails closed — if the daily budget can't be verified, the write is refused. The audit *record* (ledger write) fails open — a completed control action is never turned into an error because the audit sink hiccuped, since the caller's retry would be a second real battery write. The daemon fails fast at boot if the ledger can't open (an audit-less daemon can't do its job) but degrades gracefully if a read fails mid-loop (the gauge keeps its last value, the scrape continues).

### Usage

```sh
docker compose run --rm enphase-agent ledger --limit 50
```

The slim image ships no `sqlite3` binary, so for ad-hoc SQL use the stdlib module in the running daemon container:

```sh
docker compose exec enphase-agent python -c "import sqlite3; [print(r) for r in sqlite3.connect('/data/enphase.db').execute('SELECT ts, action, outcome, target, reason FROM writes ORDER BY ts DESC LIMIT 5')]"
```

Locally (with `sqlite3` installed): `sqlite3 enphase.db "SELECT * FROM writes ORDER BY ts DESC LIMIT 5"`.

## Architecture

**Anti-corruption layer** (`adapter.py`). The adapter is the only module that knows Enphase's shapes; callers see our `SystemState` / `BatteryMode`. When Enphase renames a field or pyenphase changes its models, the blast radius is one file.

**Fail-fast at the trust boundary, graceful degradation on the runtime path.** Bad credentials raise `AuthError` on the first call — an auth problem should never be discovered hours into a control loop. But runtime flakiness (gateway offline, cloud hiccup) degrades instead of failing: `get_state` serves the cached snapshot, flags it `stale` once it's older than 10 minutes, and stale state hard-blocks writes via `StaleStateError` — reads may be old, actuation on old data may not.

**Circuit breaker** (asyncio-native, fail_max=5, reset_timeout=300s). The IQ Gateway is a small embedded box; when it's down, pounding it helps nobody. After 5 consecutive failures the breaker opens, calls fail fast for 5 minutes, and reads fall back to the stale cache. Auth errors are excluded — a 401 is a trust-boundary problem, not an availability signal, so it fails fast instead of tripping us open.

**Token-bucket rate limiter.** A 2-second floor between outbound calls, enforced inside the adapter so no caller — including a future scheduler bug — can hammer the gateway.

**Idempotency.** `set_battery_mode` reads current state first and no-ops if the mode already matches. Battery mode writes are expensive (relay/inverter reconfiguration); retries and redundant schedules shouldn't burn them.

**Bulkhead** (`policy.py`). Max 4 mode changes per day, so a runaway caller flaps the policy layer, not the battery. The count is a query over the SQLite audit ledger, so it persists across CLI invocations and container restarts (see [Audit ledger](#audit-ledger)).

**HITL gate.** `FULL_BACKUP` needs an explicit `confirm=True` (CLI: `--confirm`). It's the one mode that visibly changes household behavior (stops self-consumption, charges from grid), so a human stays in the loop.

**Strategy table / policy-as-data** (`rules.py`). Scheduling logic is a first-match-wins list of `{when, action}` rules — reordering the list is the policy change, and every rule is unit-testable with a plain dict context. `plan_day` runs the table over 24 hours and emits the transitions.

## Tests

```sh
uv run pytest
```

pyenphase is mocked entirely; no hardware or network needed. The ledger tests use a real on-disk SQLite file per test (`tmp_path`, never `:memory:` — WAL needs a filesystem), so they exercise actual locking and snapshot isolation.
