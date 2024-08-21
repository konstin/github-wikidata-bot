FROM ubuntu AS builder

COPY --from=ghcr.io/astral-sh/uv /uv /bin/uv
ADD pyproject.toml uv.lock /
RUN mkdir -p src/github_wikidata_bot \
    && touch src/github_wikidata_bot/__init__.py \
    && UV_PYTHON_INSTALL_DIR=/python uv sync --no-dev

FROM ubuntu

COPY --from=builder /python /python
COPY --from=builder /.venv /.venv
ADD user-config.py /
ADD src /src
ENTRYPOINT ["/.venv/bin/python"]
CMD ["-m", "github_wikidata_bot"]
