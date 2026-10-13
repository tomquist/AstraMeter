# syntax=docker/dockerfile:1
ARG BUILD_FROM
ARG BUILD_VERSION

FROM ${BUILD_FROM:-ghcr.io/hassio-addons/base:17.0.1} AS builder

RUN apk add --no-cache \
    python3 \
    py3-pip

COPY --from=ghcr.io/astral-sh/uv:0.11.2 /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src/ src/

RUN uv sync --frozen --no-dev --no-editable

# uv installs no bytecode and PYTHONDONTWRITEBYTECODE stops it being cached, so
# every start would compile each dependency from source: slower, and several
# MiB of compiler garbage the process keeps resident. Alpine already ships the
# standard library compiled. Unchecked-hash pycs stay valid whatever timestamps
# the copy into the final stage leaves.
RUN python3 -m compileall -q -j 0 --invalidation-mode unchecked-hash .venv

FROM ${BUILD_FROM:-ghcr.io/hassio-addons/base:17.0.1}

ARG GIT_COMMIT_SHA=
ENV GIT_COMMIT_SHA=${GIT_COMMIT_SHA}

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apk add --no-cache python3

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH"

COPY ha_addon/run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
