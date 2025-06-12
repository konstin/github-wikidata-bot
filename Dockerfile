FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app

# Install the dependencies
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-install-project --no-dev

# Install the project itself
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

RUN groupadd -g 1000 app && useradd -u 1000 -g app app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY user-config.py src ./
USER app
ENV PATH="/app/.venv/bin:$PATH"
ENTRYPOINT ["python", "-m", "github_wikidata_bot"]
