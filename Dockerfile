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

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV LOG_LEVEL=info

ARG GIT_COMMIT_SHA=
ENV GIT_COMMIT_SHA=${GIT_COMMIT_SHA}

EXPOSE 12345/udp
EXPOSE 52500/tcp

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD curl -f http://localhost:52500/health || exit 1

USER astra

CMD ["astrameter"]
