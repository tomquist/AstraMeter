# syntax=docker/dockerfile:1
FROM rust:1.83-slim AS builder

WORKDIR /app

# Build dependencies needed for reqwest/rustls/openssl-free TLS stack.
RUN apt-get update && apt-get install -y --no-install-recommends \
        pkg-config \
        ca-certificates \
        libudev-dev \
    && rm -rf /var/lib/apt/lists/*

COPY Cargo.toml Cargo.lock rust-toolchain.toml ./
COPY .cargo .cargo
COPY crates crates
COPY bins bins

RUN cargo build --release -p astrameter-host

FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -r astra && useradd -r -g astra -s /sbin/nologin astra

WORKDIR /app

COPY --from=builder /app/target/release/astrameter /usr/local/bin/astrameter

RUN chown -R astra:astra /app

ENV LOG_LEVEL=info

ARG GIT_COMMIT_SHA=
ENV GIT_COMMIT_SHA=${GIT_COMMIT_SHA}

EXPOSE 12345/udp
EXPOSE 8080/tcp

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD curl -f http://localhost:8080/health || exit 1

USER astra

CMD ["astrameter"]
