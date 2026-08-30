# Changelog

## [0.1.0] - unreleased

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
