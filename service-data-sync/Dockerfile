FROM ghcr.io/astral-sh/uv:0.6.17 AS uv

FROM python:3.13-slim-bookworm AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_PROGRESS=1

WORKDIR /app

COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

FROM builder AS test

ENV COVERAGE_FILE=/tmp/.coverage \
    HOME=/tmp \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTEST_ADDOPTS="-p no:cacheprovider" \
    RUFF_CACHE_DIR=/tmp/ruff-cache \
    XDG_CACHE_HOME=/tmp/cache

COPY tests ./tests
RUN apt-get update \
    && apt-get install --no-install-recommends --yes libatomic1 \
    && rm -rf /var/lib/apt/lists/* \
    && uv sync --frozen --all-groups \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --home-dir /app app \
    && mkdir -p /tmp/cache /tmp/ruff-cache \
    && chown -R app:app /tmp/cache /tmp/ruff-cache

ENV UV_NO_SYNC=1

USER app

CMD ["pytest"]

FROM python:3.13-slim-bookworm AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --home-dir /app app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv .venv
COPY --from=builder --chown=app:app /app/src ./src

USER app

CMD ["data-sync-diagnostics", "--format", "console"]

FROM runtime AS development

CMD ["data-sync-worker"]
