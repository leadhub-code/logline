FROM python:3.14

COPY --from=ghcr.io/astral-sh/uv:0.11.18 /uv /uvx /bin/

WORKDIR /src

# Install dependencies first, in their own layer, so changing the source does
# not invalidate the cached dependency install.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Then copy the source and install the project itself. LICENSE is required
# because pyproject declares license-files = ["LICENSE"], which uv_build
# validates while building the project.
COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev

ENV PATH="/src/.venv/bin:$PATH"

ENTRYPOINT ["logline-server"]
