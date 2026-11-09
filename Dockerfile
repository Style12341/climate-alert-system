# One image for all four processes. It has no CMD on purpose: compose.yaml
# names the command of each service, and no process is a natural default.

FROM python:3.14-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv

# UV_COMPILE_BYTECODE: the runtime user cannot write .pyc files into the
# root-owned virtual environment, so they are produced here.
# UV_LINK_MODE=copy: the uv cache and the target are on different layers.
# UV_PYTHON_DOWNLOADS=never: the venv must use the image's interpreter, the one
# that also exists at the same path in the runtime stage.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, so a source change does not reinstall them.
COPY pyproject.toml uv.lock ./
RUN ["uv", "sync", "--locked", "--no-dev", "--no-install-project"]

COPY src ./src
# --no-editable installs the package into site-packages, so the runtime stage
# needs only the virtual environment and not the source tree.
RUN ["uv", "sync", "--locked", "--no-dev", "--no-editable"]


FROM python:3.14-slim AS runtime

RUN ["useradd", "--uid", "10001", "--no-create-home", "--shell", "/usr/sbin/nologin", "app"]

WORKDIR /app

COPY --from=builder /app/.venv ./.venv
# The migrate service runs Alembic from this same image.
COPY alembic.ini ./
COPY migrations ./migrations

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# The upgrade takes Debian fixes the base image does not have yet. pip is
# removed because its vendored packages carry CVEs and nothing runs it: uv
# built the virtual environment in the builder stage.
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/* \
    && /usr/local/bin/python -m pip uninstall --yes pip

USER app
