FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app

# Install the dependencies
ADD pyproject.toml uv.lock /app/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

ADD pyproject.toml uv.lock /app/
RUN uv sync --no-dev --no-install-project --locked

# Install the project itself
ADD src /app/src
ADD pyproject.toml uv.lock /app/
RUN uv sync --no-dev --locked

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

RUN mkdir /app
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
ADD user-config.py /app/
ADD src /app/src
ENTRYPOINT ["/app/.venv/bin/python"]
CMD ["-m", "github_wikidata_bot"]
