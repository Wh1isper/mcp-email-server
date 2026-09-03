# Container image for mcp-email-server.
#
# A Dockerfile existed until #203 ("migrate project infrastructure"), where it
# was dropped along with the release workflow's publishing job. This restores a
# build, in a two-stage form so the runtime layer carries neither uv nor the
# build inputs.
#
# The virtualenv deliberately stays at /app/.venv: deployments that override
# the entrypoint address the interpreter directly (for example to run the
# server over streamable-http instead of stdio), and that path was the
# de-facto interface of the previous image.

ARG PYTHON_VERSION=3.13

FROM python:${PYTHON_VERSION}-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Dependencies first, so a source-only change does not invalidate this layer.
COPY uv.lock pyproject.toml README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY . /app
RUN uv sync --frozen --no-dev

FROM python:${PYTHON_VERSION}-slim AS runtime

# tini reaps the children the server spawns; without an init the container
# accumulates zombies on long-running IMAP sessions.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The venv holds absolute paths, so the runtime stage must use the same base
# image and the same /app prefix as the builder.
COPY --from=builder /app /app

ENV PATH="/app/.venv/bin:$PATH"

ENTRYPOINT ["tini", "--", "mcp-email-server"]
CMD ["stdio"]
