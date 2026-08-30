# Changelog

## [0.1.0] - unreleased

- Initial thin slice: adapter (anti-corruption layer over pyenphase), policy guardrails, deterministic rules engine, Typer CLI, tests.
- Live-hardware spike against IQ Gateway (pyenphase 4.0.1):
  - Confirmed `EnvoyStorageMode` members, `set_storage_mode` / `set_reserve_soc` signatures, `EnvoyData` field shapes; TODO(verify) marks cleared.
  - Replaced `pybreaker` (Tornado-only async path in 1.4.1) with an asyncio-native `_AsyncCircuitBreaker` — same semantics, no new deps.
  - Lazy-initialize `Envoy` inside async context; the aiohttp connector requires a running event loop.
  - Load `.env` on CLI import via `python-dotenv`.
  - Added `EnphaseAdapter.close()` + `__aenter__/__aexit__`; CLI now closes the aiohttp session on exit.
  - `enable_storm_guard` raises `NotImplementedError` — pyenphase 4.0.1 does not expose the write; needs a raw Enlighten cloud call in a follow-up.
- Containerization: multi-stage `Dockerfile` (uv build → python:3.11-slim runtime, non-root user), `docker-compose.yml`, `.dockerignore`. Data mounted at `/data` via a **named** Docker volume — deliberately not a bind-mount, because SQLite's fcntl advisory locks don't forward reliably through Docker Desktop's virtiofs / WSL2's 9P, so a bind-mounted DB pseudo-locks the moment the host touches the file.
