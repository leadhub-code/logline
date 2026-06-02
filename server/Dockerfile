FROM python:3.9-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.18 /uv /uvx /bin/

WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
COPY logline_server ./logline_server

RUN uv sync --frozen --no-dev

ENV PATH="/src/.venv/bin:$PATH"

ENTRYPOINT ["logline-server"]
