FROM ubuntu AS builder

RUN mkdir /app
WORKDIR /app
ENV UV_PYTHON_INSTALL_DIR=/app/python

COPY --from=ghcr.io/astral-sh/uv:0.4 /uv /bin/uv

# Install the dependencies
ADD pyproject.toml uv.lock /app/
RUN uv sync --no-dev --no-install-project --locked

# Install the project itself
ADD src /app/src
RUN uv sync --no-dev --locked

FROM ubuntu

RUN mkdir /app
WORKDIR /app
COPY --from=builder /app/python /app/python
COPY --from=builder /app/.venv /app/.venv
ADD user-config.py /app/
ADD src /app/src
ENTRYPOINT ["/app/.venv/bin/python"]
CMD ["-m", "github_wikidata_bot"]
