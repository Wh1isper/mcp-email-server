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
ARG UV_VERSION=0.11.29

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:${PYTHON_VERSION}-slim AS builder

COPY --from=uv /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Dependencies first, so a source-only change does not invalidate this layer.
COPY uv.lock pyproject.toml README.md LICENSE ./
RUN uv sync --frozen --no-install-project --no-dev

COPY mcp_email_server ./mcp_email_server
RUN uv sync --frozen --no-dev --no-editable

FROM python:${PYTHON_VERSION}-slim AS runtime

LABEL org.opencontainers.image.source="https://github.com/Wh1isper/mcp-email-server" \
      org.opencontainers.image.description="MCP server for IMAP and SMTP email workflows" \
      org.opencontainers.image.licenses="MIT"

# tini reaps the children the server spawns; without an init the container
# accumulates zombies on long-running IMAP sessions.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The venv holds absolute paths, so the runtime stage must use the same base
# image and the same /app prefix as the builder. The project is installed
# non-editably, allowing the runtime image to exclude all source and build files.
COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH"

ENTRYPOINT ["tini", "--", "mcp-email-server"]
CMD ["stdio"]
