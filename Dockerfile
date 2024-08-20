FROM ubuntu AS builder

COPY --from=ghcr.io/astral-sh/uv /uv /bin/uv
ADD pyproject.toml pyproject.toml
ADD uv.lock uv.lock
RUN mkdir -p src/github_wikidata_bot \
    && touch src/github_wikidata_bot/__init__.py \
    && UV_PYTHON_INSTALL_DIR=/python uv sync --no-dev

FROM ubuntu

COPY --from=builder /python /python
COPY --from=builder /.venv /.venv
ADD src src
ADD user-config.py user-config.py
CMD [".venv/bin/python", "-m", "github_wikidata_bot"]
