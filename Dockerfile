FROM ubuntu AS builder

COPY --from=ghcr.io/astral-sh/uv /uv /bin/uv
RUN mkdir /app
WORKDIR /app
ADD pyproject.toml uv.lock /app/
RUN mkdir -p src/github_wikidata_bot \
    && touch src/github_wikidata_bot/__init__.py \
    && UV_PYTHON_INSTALL_DIR=/app/python uv sync --no-dev

FROM ubuntu

RUN mkdir /app
WORKDIR /app
COPY --from=builder /app/python /app/python
COPY --from=builder /app/.venv /app/.venv
ADD user-config.py /app/
ADD src /app/src
ENTRYPOINT ["/app/.venv/bin/python"]
CMD ["-m", "github_wikidata_bot"]
