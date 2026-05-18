# Agent notes

This is a Rust workspace. The previous Python implementation under
`src/astrameter/` was removed in version 3.0.0; if you need the legacy
reference, check out git history before the Phase 9 commit (search for
"Phase 9: delete Python source").

The migration plan that produced the current layout lives at
`/root/.claude/plans/migrate-this-project-to-witty-globe.md`.

## Workspace layout

- `crates/core` — `Powermeter` trait, fundamental types, error enum.
- `crates/config` — INI parser + section dispatch helpers.
- `crates/platform` — async traits (HTTP, WebSocket, MQTT, UDP, Serial, Timer).
- `crates/platform-std` — host implementations.
- `crates/platform-espidf` — ESP32 implementations (cfg-gated; Phase 8 WIP).
- `crates/powermeters` — all 17 powermeter implementations + runtime registry.
- `crates/wrappers` — transform / throttling / smoothing / hampel / pid.
- `crates/emulator-shelly` — Shelly UDP emulator.
- `crates/emulator-ct002` — Marstek CT002/CT003 UDP emulator.
- `crates/insights-mqtt` — MQTT Insights + HA discovery service.
- `crates/marstek-api` — Marstek cloud HTTPS client.
- `crates/web` — health + config editor.
- `crates/sml` — Smart Meter Language (SML) decoder.
- `crates/testkit` — shared test fixtures.
- `bins/astrameter-host` — Linux/Docker binary.
- `bins/astrameter-esp32` — ESP32-S3 firmware (cross-compile only).

## Development commands

Resolved versions live in **`Cargo.lock`**. Before finishing Rust changes,
run (from repo root):

```bash
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace --all-targets
cargo test --workspace --doc
```

CI: `.github/workflows/rust-ci.yml` runs the equivalent on PRs.

The ESP32 firmware is excluded from default workspace members so
`cargo check` works on a stock Linux host. Build it with:

```bash
cargo +esp build -p astrameter-esp32 --target xtensa-esp32s3-espidf
```

(Requires `espup install` for the Xtensa toolchain.)

## Changelog

For user-facing work on a branch, keep **one bullet under `## Next`** that
summarizes the **overall** outcome of that branch. **Add** it when you
first document the change; on **later iterations** on the same branch,
**edit that same bullet** if the scope or wording shifts — do **not**
append extra bullets for each follow-up. Skip `CHANGELOG.md` entirely
when nothing users would notice changes (refactors, tests-only, etc.).

## Adding a powermeter

1. **Implementation** — Add `crates/powermeters/src/<name>.rs` with a
   struct implementing the `astrameter_core::Powermeter` trait. Polling
   meters override `get_powermeter_watts`; push-based meters override
   `start`, `stop`, and `wait_for_next_message` as well.
2. **Factory** — Export a `pub fn create(section: &Section<'_>,
   platform: Arc<Platform>) -> Result<Arc<dyn Powermeter>>` from the
   same module.
3. **Registry** — Add one line to `register_all()` in
   `crates/powermeters/src/lib.rs` with the INI section-name prefix
   that selects this meter. Order matters when one prefix is a prefix
   of another (e.g. `MQTT_INSIGHTS` before bare `MQTT`).
4. **Config example & docs** — Add a commented section to
   `config.ini.example` and a subsection under **Configuration** in
   `README.md`, plus one `## Next` bullet in `CHANGELOG.md` (see
   Changelog above).
5. **Tests** — Add `#[cfg(test)] mod tests` blocks alongside the
   implementation. The HTTP-driven meters can use `wiremock` (already
   in `[dev-dependencies]`) for integration tests against a fake HTTP
   server.

`POWER_OFFSET` / `POWER_MULTIPLIER`, `THROTTLE_INTERVAL`,
`SMOOTH_TARGET_ALPHA`, `MAX_SMOOTH_STEP`, `DEADBAND`, `HAMPEL_*`,
`PID_*`, `WAIT_FOR_NEXT_MESSAGE`, and `NETMASK` are applied globally by
the supervisor for any section that returns a powermeter — no extra
wiring needed in your new module.
