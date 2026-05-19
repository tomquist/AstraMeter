# Contributing

Thanks for helping improve AstraMeter. This document covers local development; for end-user install options see [README.md](README.md).

## Prerequisites

- **Stable Rust** 1.83 or newer (install via [rustup](https://rustup.rs/))
- **cargo-nextest** (optional, faster test runner): `cargo install cargo-nextest`
- **Xtensa toolchain** (only if you want to build the ESP32-S3 firmware): `cargo install espup espflash && espup install`

## Dev setup

Clone the repo and let Cargo fetch dependencies on first build:

```bash
cargo build --workspace
```

The ESP32 binary (`bins/astrameter-esp32`) is excluded from the default workspace members so `cargo check` works on a stock Linux host. Build it explicitly with `cargo +esp build -p astrameter-esp32 --target xtensa-esp32s3-espidf`.

## Project layout

This is a Cargo workspace. Notable pieces:

| Path | Role |
|------|------|
| `crates/core` | `Powermeter` trait, error enum, fundamental types |
| `crates/config` | INI parser + section dispatch helpers |
| `crates/platform` | Async transport traits (HTTP, WebSocket, MQTT, UDP, Serial, Timer, …) |
| `crates/platform-std` | Host implementations (reqwest, tokio, axum, socket2, …) |
| `crates/platform-espidf` | ESP32 implementations (cfg-gated; only built for `xtensa-esp32s3-espidf`) |
| `crates/powermeters` | All 17 powermeter implementations + the runtime registry |
| `crates/wrappers` | Transform / throttle / smoothing / Hampel / PID |
| `crates/emulator-ct002` | Marstek CT002/CT003 UDP emulator + load balancer |
| `crates/emulator-shelly` | Shelly UDP emulator |
| `crates/insights-mqtt` | MQTT Insights + Home Assistant Device Discovery + Marstek MQTT bridge |
| `crates/marstek-api` | Marstek cloud HTTPS client |
| `crates/web` | Health endpoint + web config editor |
| `crates/sml` | SML / IEC 62056-7-5 decoder |
| `crates/testkit` | Shared test fixtures |
| `bins/astrameter-host` | Linux/Docker binary |
| `bins/astrameter-esp32` | ESP32-S3 firmware (cross-compile only) |

Resolved versions live in `Cargo.lock`.

## Checks to run before pushing

From the repo root:

```bash
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace --all-targets
cargo test --workspace --doc
```

(Or substitute `cargo nextest run --workspace` for `cargo test`.) CI runs the equivalents in `.github/workflows/rust-ci.yml`.

## Adding a powermeter

Follow the checklist in [AGENTS.md](AGENTS.md) (**Adding a powermeter**):

1. Add `crates/powermeters/src/<name>.rs` with a struct implementing `astrameter_core::Powermeter`.
2. Export a `pub fn create(section: &Section<'_>, platform: Arc<Platform>) -> Result<Arc<dyn Powermeter>>` from the same module.
3. Register the INI section-name prefix in `register_all()` in `crates/powermeters/src/lib.rs` (order matters when one prefix is a prefix of another, e.g. `MQTT_INSIGHTS` before bare `MQTT`).
4. Add a commented section to `config.ini.example` and a subsection under **Configuration** in `README.md`.
5. Add `#[cfg(test)] mod tests` blocks alongside the implementation. The HTTP-driven meters can use `wiremock` (already in `[dev-dependencies]`) for integration tests against a fake HTTP server.

`POWER_OFFSET` / `POWER_MULTIPLIER`, `THROTTLE_INTERVAL`, `SMOOTH_TARGET_ALPHA`, `MAX_SMOOTH_STEP`, `DEADBAND`, `HAMPEL_*`, `PID_*`, `WAIT_FOR_NEXT_MESSAGE`, and `NETMASK` are applied globally by the supervisor for any section that returns a powermeter — no extra wiring needed in your new module.

## Branches and pull requests

- Base feature work on **`develop`** and open PRs against **`develop`**.
- Releases are merged to **`main`** as appropriate for the project maintainer.

## Changelog

For user-visible changes, add or update the single bullet under **`## Next`** in [CHANGELOG.md](CHANGELOG.md) (see [AGENTS.md](AGENTS.md) — Changelog).
