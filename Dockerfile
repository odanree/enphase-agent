# Multi-stage: uv builds a reproducible venv from the pinned lock, then we
# copy just what runtime needs into a slim final image. Keeps the wheel of
# build tools out of the shipped layer.

FROM python:3.11-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Deps first — better layer caching when only app code changes. README is
# read by hatchling as project metadata during the install step.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

# Then app source + install the project itself.
COPY enphase_agent ./enphase_agent
RUN uv sync --frozen --no-dev


FROM python:3.11-slim AS runtime

# Non-root runtime user. Container escape is not a real concern for a LAN
# agent, but running as PID 1 root is bad hygiene and confuses volume
# ownership when the same volume is later exec'd into as root.
RUN groupadd --system app && useradd --system --gid app --home /app --shell /sbin/nologin app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/enphase_agent /app/enphase_agent
COPY --from=builder --chown=app:app /app/pyproject.toml /app/pyproject.toml

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ENPHASE_DB_PATH=/data/enphase.db \
    TZ=America/Los_Angeles

# Volume mount point owned by the app user. compose attaches a *named*
# volume here — NOT a bind-mount — because Docker Desktop's virtiofs (and
# WSL2's 9P before it) does not reliably forward SQLite's fcntl advisory
# locks across the host boundary, so a bind-mounted DB gets pseudo-locked
# the moment anything on the host touches the file.
RUN mkdir -p /data && chown app:app /data
VOLUME ["/data"]

USER app

# Prometheus scrapes this port (pull-based metrics — we never push).
EXPOSE 8000

ENTRYPOINT ["enphase-agent"]
CMD ["daemon"]
