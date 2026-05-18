# syntax=docker/dockerfile:1
ARG BUILD_FROM
ARG BUILD_VERSION

FROM rust:1.83-slim AS builder

WORKDIR /app

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

FROM ${BUILD_FROM:-ghcr.io/hassio-addons/base:17.0.1}

ARG GIT_COMMIT_SHA=
ENV GIT_COMMIT_SHA=${GIT_COMMIT_SHA}

RUN apk add --no-cache ca-certificates

WORKDIR /app

COPY --from=builder /app/target/release/astrameter /usr/local/bin/astrameter

COPY ha_addon/run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
