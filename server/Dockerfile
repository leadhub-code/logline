FROM python:3.9-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

COPY logline_server /src/logline_server
COPY pyproject.toml README.md /src/

RUN uv pip install --system /src

ENTRYPOINT ["logline-server"]
