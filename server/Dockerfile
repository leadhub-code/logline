FROM python:3.9-slim

# tini as PID 1: forwards signals to the worker supervisor and reaps it.
# (The supervisor in turn reaps its forked worker processes.)
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

COPY logline_server /src/logline_server
COPY pyproject.toml README.md /src/

RUN uv pip install --system /src

# Workers scale within this single container's network namespace, which is
# what SO_REUSEPORT requires. Pass e.g. `--workers auto` (one per CPU) as args.
ENTRYPOINT ["tini", "--", "logline-server"]
