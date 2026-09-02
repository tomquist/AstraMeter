---
description: Keeps the Python CT002 stack and its ESPHome C++ mirror in step: what must change on both sides, which divergences are deliberate, what the ESP32 constrains, and which tests prove parity. Use when changing CT002 or balancer behavior, adding a status field or a dashboard control, or editing the ESPHome component.
---

# CT002 parity

`esphome/components/ct002/` is a mechanical C++ mirror of the Python CT002
stack. Shared behavior lands on **both** sides in the same change;
`CONTRIBUTING.md` maps file to file.

## What is mirrored

- **Status document** — `status/serialize.py` ↔ `status_json.{h,cpp}`, plus the
  `status_snapshot` methods on `CT002` and `LoadBalancer`. Same field names and
  units. The firmware sends a deliberately reduced document: every field is
  optional, the page renders only what arrives and never substitutes 0 or "—",
  and what it omits is what the page does not render (`balancer.config`, and
  the `integrations` entries with no card). A field the page *does* render
  belongs on both sides.
- **Write path** — `ct002/controls.py` ↔ `controls.{h,cpp}`. The bounds MUST
  match, or a value one stack accepts is silently reverted by the other's next
  retained MQTT replay. That one table also feeds the MQTT command handlers and
  the Home Assistant discovery payloads, so a bound exists once per stack rather
  than once per surface. Both stacks require `Content-Type: application/json` on
  writes and compare the parsed media type, never a substring — see the
  `JSON_CONTENT_TYPE` comment in `web_server.py` for what a substring test
  lets through. Python enforces it in `WebServer._add`, so every `POST` route
  is covered by construction; the firmware calls `controls::is_json_content_type`,
  which lives in `controls.{h,cpp}` rather than `dashboard.cpp` so a host gtest
  can drive it. Keep both there. Firmware controls are opt-in (`controls:`,
  default off) because that page has no login.

  Three divergences are deliberate — don't "restore" them. The firmware rejects
  a device write with no `value` (except the `force_rotation` button, which
  carries none), requires a JSON number where Python accepts anything `float()`
  swallows, and ignores `device_id`, having one device to write to.
- **Configuration** — permanently waived. An ESPHome device's config is compiled
  into its firmware, so there is nothing for a dashboard to write; the page hides
  its Configuration tab when the backend reports no `config_mode`.

## What the firmware constrains

The dashboard is one page served by both stacks (see the `build-dashboard` skill), and
the ESP32 half sets its limits:

- It lives in flash, inside the gzipped budget `npm run check:dashboard`
  enforces.
- The HTTP handler runs on the httpd task, **not** the main loop, so it must
  never walk live state: `dashboard.cpp` builds its document in `loop()` and
  hands writes to it, both across a mutex.
- The ESP-IDF HTTP shim parses only form-encoded POST bodies, so
  `handle_control_` reads the JSON body off the raw `httpd_req_t` itself. Keep it
  that way rather than inventing a second wire format.

## Running the suite

```bash
uv run pytest tests/components/ct002/
```

ESPHome has no web server for the `host` platform, so `dashboard.cpp` compiles
**only** in the ESP32 matrix. Run that yourself after touching it — don't leave
it to CI:

```bash
cd tests/components/ct002 && esphome compile test.dashboard.esp32-idf.yaml
```

Everything else about the firmware is covered by `host_status_json_test.cpp` and
`host_controls_test.cpp` (wire format and write-path bounds),
`host_write_slot_test.cpp` (the httpd-task ↔ main-loop handover, with real
threads — which is why that handover lives in `write_slot.h`, free of ESPHome
deps, since the race is otherwise untestable) and `test_dashboard_e2e.py` (live
state and writes, against the compiled host binary over the `status` / `control`
test-control channel in `test_hooks.cpp`).

## Sandbox setup

All three are obtainable — don't report a suite as skipped without trying:

- **ESPHome CLI** — `uv tool install esphome` (~2 min). Without it
  `test_shared_e2e.py` skips ~40 tests, and those are the ones that actually
  prove parity.
- **googletest** behind a TLS-intercepting proxy — `test_host_protocol.py`
  fails to build because the tarball fetch gets a 403. `git clone` does not:

  ```bash
  git clone --depth 1 -b v1.14.0 https://github.com/google/googletest.git /tmp/googletest
  cmake -S tests/components/ct002 -B /tmp/ct002_build -DCMAKE_BUILD_TYPE=Release \
        -DFETCHCONTENT_SOURCE_DIR_GOOGLETEST=/tmp/googletest
  cmake --build /tmp/ct002_build -j && (cd /tmp/ct002_build && for t in host_*_test; do ./$t; done)
  ```
- **PlatformIO** overrides `REQUESTS_CA_BUNDLE` with `certifi.where()`, so the
  proxy CA has to go into *that* bundle:
  `cat /root/.ccr/ca-bundle.crt >> /root/.platformio/penv/lib/python3.11/site-packages/certifi/cacert.pem`.
  A config then compiles in ~2.5 min.
