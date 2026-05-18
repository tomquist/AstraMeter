# Agent notes

This repository is in the middle of a Python → Rust migration with ESP32
support. See `/root/.claude/plans/migrate-this-project-to-witty-globe.md` for
the full plan. Both stacks coexist on disk during the transition:

- **Python** code lives in `src/astrameter/` and remains the reference
  implementation until cutover (Phase 9 of the plan).
- **Rust** code lives in `crates/` and `bins/`, organised as a Cargo
  workspace (see top-level `Cargo.toml`). Phase 0 is a skeleton only; real
  implementations land in Phases 1 onwards.

## Python (still active)

Resolved versions live in **`uv.lock`**. Install dev dependencies the same way CI does:

```bash
uv sync --extra dev
```

Before finishing Python changes, run (from repo root, with dev deps):

```bash
uv run ruff format .
uv run ruff check .
uv run mypy src/
uv run pytest
```

Python CI: `.github/workflows/ci.yml`.

## Rust (new, alongside Python)

Resolved versions live in **`Cargo.lock`**. Before finishing Rust changes,
run (from repo root):

```bash
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace --all-targets
cargo test --workspace --doc
```

Rust CI: `.github/workflows/rust-ci.yml`. The ESP32 cross-compile job is
commented out until Phase 8.

The ESP32 binary (`bins/astrameter-esp32`) is excluded from the default
workspace members so `cargo check` works on a stock host. Build it with:

```bash
cargo +esp build -p astrameter-esp32 --target xtensa-esp32s3-espidf
```

(requires `espup` and the Xtensa toolchain — Phase 8 onwards).

## Changelog

For user-facing work on a branch, keep **one bullet under `## Next`** that summarizes the **overall** outcome of that branch. **Add** it when you first document the change; on **later iterations** on the same branch, **edit that same bullet** if the scope or wording shifts—do **not** append extra bullets for each follow-up. Skip `CHANGELOG.md` entirely when nothing users would notice changes (refactors, tests-only, etc.).

Do **not** expand `CHANGELOG.md` with every internal or tooling-only follow-up. If the branch bullet already states the high-level theme, leave it unless the **user-visible** story changes.

## Adding a powermeter

1. **Implementation** — Add `src/astrameter/powermeter/<module>.py` with a class subclassing `Powermeter`; implement `get_powermeter_watts()` (and `wait_for_message()` only if the base default is wrong for your source).
2. **Exports** — Import and re-export the class from `src/astrameter/powermeter/__init__.py`.
3. **Config** — In `src/astrameter/config/config_loader.py`: import the class, define a `*_SECTION` string, add a `section.startswith(...)` branch in `create_powermeter()`, and a `create_*_powermeter()` factory that reads options from the section. `POWER_OFFSET` / `POWER_MULTIPLIER`, `THROTTLE_INTERVAL`, and `NETMASK` are handled globally for any section that returns a powermeter — no extra wiring unless you need something custom.
4. **Examples, docs & changelog** — Add a commented example to `config.ini.example` and a subsection under **Configuration** in `README.md`, plus one **`## Next`** bullet for the powermeter (add once, then update that bullet on follow-up iterations if needed—see **Changelog** above).
5. **Tests** — Add `src/astrameter/powermeter/<module>_test.py` (or extend existing tests) and run the commands above before finishing.
