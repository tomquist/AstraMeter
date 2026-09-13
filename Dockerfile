# syntax=docker/dockerfile:1
FROM python:3.12-slim AS builder

# cffi (pulled in transitively via aioesphomeapi -> cryptography) ships no
# prebuilt wheel for linux/arm/v7, so it compiles from source and needs a full C
# toolchain (compiler, libc headers + startup objects) plus the libffi headers.
# cryptography itself has an armv7 wheel, so no Rust toolchain is required here.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libffi-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "uv==0.11.2"

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src/ src/

RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -r astra && useradd -r -g astra -s /sbin/nologin astra

WORKDIR /app

COPY --from=builder /app /app

RUN chown -R astra:astra /app

# Let the non-root user bind the privileged port the Shelly HTTP surface uses.
# Ordering matters: `chown -R` above clears extended attributes, so the
# capability has to be set after it or it silently disappears. The guard pipes
# through grep because bare `getcap` exits 0 and prints nothing when a file
# has no capability, so it cannot fail a build on its own.
RUN cp "$(readlink -f /app/.venv/bin/python3)" /app/.venv/bin/python-cap \
    && chown astra:astra /app/.venv/bin/python-cap \
    && apt-get update \
    && apt-get install -y --no-install-recommends libcap2-bin \
    && setcap cap_net_bind_service=+ep /app/.venv/bin/python-cap \
    && getcap /app/.venv/bin/python-cap | grep -q cap_net_bind_service \
    && apt-get purge -y --auto-remove libcap2-bin \
    && rm -rf /var/lib/apt/lists/*

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV LOG_LEVEL=info

ARG GIT_COMMIT_SHA=
ENV GIT_COMMIT_SHA=${GIT_COMMIT_SHA}

EXPOSE 12345/udp
EXPOSE 52500/tcp
# The Shelly HTTP surface, and mDNS so batteries can discover it. Both need
# host networking to be reachable — see docs/installation/docker.md.
EXPOSE 80/tcp
EXPOSE 5353/udp

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD curl -f http://localhost:52500/health || exit 1

USER astra

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["astrameter"]
