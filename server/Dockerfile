FROM python:3.14

COPY --from=ghcr.io/astral-sh/uv:0.11.18 /uv /uvx /bin/

WORKDIR /src

# Install dependencies first, in their own layer, so changing the source does
# not invalidate the cached dependency install.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra otel

# Then copy the source and install the project itself. LICENSE is required
# because pyproject declares license-files = ["LICENSE"], which uv_build
# validates while building the project.
COPY README.md LICENSE ./
COPY logline_server ./logline_server

# Record the git commit the image is built from, shown by `--version`. The
# build context does not include .git, so it has to be passed in, e.g.
# `--build-arg GIT_COMMIT=$(git rev-parse HEAD)`. Only hex digits (optionally
# followed by "-dirty") are accepted so the value is safe to put into the code.
ARG GIT_COMMIT=
RUN if [ -n "$GIT_COMMIT" ]; then \
        echo "$GIT_COMMIT" | grep -Eqx '[0-9a-f]{7,64}(-dirty)?' || { echo "Invalid GIT_COMMIT: $GIT_COMMIT" >&2; exit 1; }; \
        sed -i "s/^git_commit = None\$/git_commit = '$GIT_COMMIT'/" logline_server/version.py; \
        grep -qx "git_commit = '$GIT_COMMIT'" logline_server/version.py; \
    fi

RUN uv sync --frozen --no-dev --extra otel

ENV PATH="/src/.venv/bin:$PATH"

ENTRYPOINT ["logline-server"]
