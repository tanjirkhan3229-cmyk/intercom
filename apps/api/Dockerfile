# Relay API image — build once, tagged by SHA in CI (RFC-001 §13).
# Runs the FastAPI `app`, the Celery `worker`/`beat`, and the one-shot `migrate` step.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# uv: fast, reproducible installs (RFC-001 §6.1 mandates uv).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Layer 1: resolve + install dependencies only (cached until pyproject changes).
COPY apps/api/pyproject.toml ./
RUN uv pip compile pyproject.toml -o /tmp/requirements.txt \
    && uv pip install --system --no-cache -r /tmp/requirements.txt

# Layer 2: source + install the package itself (no deps — already installed).
COPY apps/api/ ./
RUN uv pip install --system --no-cache --no-deps -e .

ENV PYTHONPATH=/app/src
EXPOSE 8000

# Default: the API tier. worker/beat/migrate override `command` in compose.
CMD ["uvicorn", "relay.main:app", "--host", "0.0.0.0", "--port", "8000"]
