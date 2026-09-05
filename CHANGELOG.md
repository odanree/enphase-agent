# Changelog

## [0.1.0] - unreleased

- SQLite audit ledger (`ledger.py`, `aiosqlite`):
  - One `writes` table at `ENPHASE_DB_PATH` (`/data/enphase.db` on the named volume), WAL mode — concurrent-reader-single-writer safe across the CLI/daemon process boundary. `CREATE IF NOT EXISTS` only; no migration framework until a second schema shape exists.
  - `BatteryPolicy`'s per-day mode-change bulkhead now persists across process boundaries and container restarts (previously an in-memory list that reset on every CLI invocation). The bulkhead check fails closed on a ledger read error; the audit record fails open (logged) so an audit hiccup can't turn a completed battery write into a retried one.
  - Write-through instrumentation: one `BatteryPolicy` call site fans out to both `enphase_writes_total` and the ledger; the recorder signature grew `target` / `reason` / `error_class` (dropped by the metrics sink — cardinality discipline).
  - Daemon publishes `enphase_writes_last_24h{action,outcome}` from the ledger every scrape (audit trail as materialized view, zero-filled over the closed label vocabulary), closing the daemon-can't-see-CLI-writes gap that left the Grafana audit metric permanently 0. Ledger open failure at daemon boot is fatal (fail-fast at the trust boundary); a read failure mid-loop is logged and the gauge keeps its last value.
  - New `enphase-agent ledger [--limit N]` CLI command (read-only Rich table). `LazyGauge` gained optional labels.

- Historical / cumulative metrics + "Today at a glance" dashboard row:
  - Historical energy metrics from pyenphase accumulators: today / 7d / lifetime watt-hours for production and consumption (`EnvoySystemConsumption` confirmed to carry the same accumulator shape as production; consumption series only exist on CT-metered gateways).
  - Battery energy metrics: `enphase_battery_energy_available_watt_hours` (usable now) and `enphase_battery_energy_capacity_watt_hours` (nameplate), from the Encharge aggregate.
  - Grid flow via **directional split gauges**: one signed net-consumption reading becomes `enphase_grid_import_watts` / `enphase_grid_export_watts`, both always ≥ 0, at most one nonzero.
  - All optional-source gauges are lazy (`LazyGauge`): absent from `/metrics` until the gateway actually reports a value — no phantom zeros from non-CT gateways; a transiently missing field keeps the last value (freshness stays `enphase_state_age_seconds`' job).
  - Prometheus recording rules (`prometheus/rules.yml`, 1m cadence) as **materialized views**: `enphase:solar_coverage_24h:ratio`, `enphase:time_in_mode_24h:seconds` (info-gauge integrated over time), `enphase:soc_peak_24h:ratio` / `enphase:soc_valley_24h:ratio`.
  - Prometheus retention extended 15d → **1y** (hot-storage-only, ~1 GB/yr at this cardinality); `--web.enable-lifecycle` enables `POST /-/reload` for rule iteration without restarts.
  - 5 new Grafana panels on the existing dashboard (same UID): Today-at-a-glance stat row (produced / consumed / solar coverage / grid flow) + time-in-mode pie.

- Observability stack:
  - Prometheus `/metrics` endpoint (`prometheus_client`): RED-method service metrics for the adapter (scrape rate / errors-by-kind / duration histogram, breaker state) plus domain gauges for the battery (production, consumption, SoC, reserve, one-hot mode, storm guard, `state_age_seconds` freshness SLI). Closed label vocabularies enforced in `metrics.py` (cardinality discipline).
  - `daemon` CLI command / `daemon.py`: pull-based scraper loop (15s), bulkheaded per iteration — a failed `get_state` bumps `enphase_scrape_errors_total{kind=...}` and the loop continues; SIGTERM/SIGINT shut down cleanly (adapter session closed, exit 0). Container default CMD is now `daemon`.
  - `enphase_writes_total{action,outcome}` audit counter bumped from the policy layer via an injected recorder (tests stub it; success/rejected/error outcomes).
  - Grafana + Prometheus sidecars in compose: all mutable state (Prometheus TSDB, Grafana DB, agent data) on **named volumes**; config and provisioning files are read-only bind mounts (safe — the virtiofs/9P fcntl bug only affects lock-hungry databases).
  - Starter 3-panel provisioned dashboard (stable UID): Solar vs Consumption, Battery SoC gauge, Health SLI (state age + error rate); every panel description explains its PromQL and what unhealthy looks like.

- Initial thin slice: adapter (anti-corruption layer over pyenphase), policy guardrails, deterministic rules engine, Typer CLI, tests.
- Live-hardware spike against IQ Gateway (pyenphase 4.0.1):
  - Confirmed `EnvoyStorageMode` members, `set_storage_mode` / `set_reserve_soc` signatures, `EnvoyData` field shapes; TODO(verify) marks cleared.
  - Replaced `pybreaker` (Tornado-only async path in 1.4.1) with an asyncio-native `_AsyncCircuitBreaker` — same semantics, no new deps.
  - Lazy-initialize `Envoy` inside async context; the aiohttp connector requires a running event loop.
  - Load `.env` on CLI import via `python-dotenv`.
  - Added `EnphaseAdapter.close()` + `__aenter__/__aexit__`; CLI now closes the aiohttp session on exit.
  - `enable_storm_guard` raises `NotImplementedError` — pyenphase 4.0.1 does not expose the write; needs a raw Enlighten cloud call in a follow-up.
- Containerization: multi-stage `Dockerfile` (uv build → python:3.11-slim runtime, non-root user), `docker-compose.yml`, `.dockerignore`. Data mounted at `/data` via a **named** Docker volume — deliberately not a bind-mount, because SQLite's fcntl advisory locks don't forward reliably through Docker Desktop's virtiofs / WSL2's 9P, so a bind-mounted DB pseudo-locks the moment the host touches the file.
