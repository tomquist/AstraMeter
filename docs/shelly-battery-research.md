# Shelly-speaking batteries: research notes

Developer notes behind the [battery table](shelly-tcp.md#batteries): what each
battery that reads a Shelly Pro 3EM over the network is known to need, where
that knowledge comes from, what turned out not to matter, and a worked design
for serving several batteries that each need their own meter. Written so the
next change in this area can start from here instead of redoing the research.

**Status (September 2026):** nothing here has been confirmed with AstraMeter on
real hardware. Every claim is someone else's field report, a vendor's pairing
guide, or the source of another emulator reported to work — the confidence
column says which.

## Sources, and how to reach them again

| Kind | What it is good for | Reachable from an agent sandbox? |
|---|---|---|
| Vendor pairing guides (Growatt, Solakon, Indevolt, Anker, Zendure) | Whether a battery reads the meter locally at all, or only through a Shelly Cloud login | Yes. Growatt's is a PDF — extract the text with `pypdf`. |
| Source of emulators reported to work: [Energy2Shelly_ESP](https://github.com/TheRealMoeder/Energy2Shelly_ESP), [ottelo's Tasmota script](https://github.com/ottelo9/tasmota-sml-script) and the [Tasmota scripter's mDNS code](https://github.com/arendst/Tasmota/blob/development/tasmota/tasmota_xdrv_driver/xdrv_10_scripter.ino) | What a battery demonstrably accepts: exact responses, headers, mDNS names and TXT | Yes — `git clone` works. |
| Change history of another open-source (Java) emulator that lists these batteries as supported | Which fix each battery needed, and when | Yes — clone it with `--filter=blob:none` and use `git log -S` / `git show`. Its issue bodies are mostly empty; the commits are the record. |
| [photovoltaikforum.com](https://www.photovoltaikforum.com/) threads (the richest field reports) | Request logs and pairing experience | **No.** The forum's host answers 403 to datacenter addresses, the crawler skill included. Only search-result excerpts get through. Read it from a residential connection if a detail matters. |
| Zendure community forum | Zendure pairing reports | Not as HTML (it is a single-page app), but its JSON API is: `https://flarum-eu.zendure.tech/api/discussions/<id>`. |
| GitHub search API | Finding issues in other repositories | No — agent sessions are scoped to this repository. Clone instead. |

## Per battery

| Battery | Pairing path | Transport after pairing | Port | Needs | Confidence |
|---|---|---|---|---|---|
| **Hoymiles** MS-A2, HiBattery 1920 AC / 4020-X | mDNS discovery | HTTP, polling | **80, ignores the announced port** | `EM.GetConfig` (newer firmware); `Content-Length` | High: several emulators, fixes named after it |
| **Growatt** NOAH 2000, NEXA 2000 | mDNS discovery via the ShinePhone option "Shelly Pro 3EM *in the same network*" | HTTP, polling | **80, ignores the announced port** | Cloud reported as disabled (`Cloud.GetStatus`, `Shelly.GetStatus.cloud`) | High for the requirements; Growatt's own guide (Nov 2024) describes only the Shelly-account path, which cannot work |
| **Solakon** One | mDNS discovery; manual address as fallback | HTTP (details unknown) | 80 assumed | The naming below: instance and SRV host `ShellyPro3EM-<MAC>`, TXT `id` lowercase, TXT `mac` colon-separated uppercase | Medium: one emulator's fix titled "to allow the pairing with the Solakon One" |
| **Indevolt** SolidFlex 2000, BK1600 | Manual: address **and device ID** | Local LAN (details unknown) | 80 assumed | The device ID the user types must be the one we report (`Shelly.GetDeviceInfo.id`). **One meter pairs with one Indevolt system.** | Medium: vendor guide; listed as supported by another emulator |
| **Anker** SOLIX Solarbank 2 / 3 | Official: Shelly Cloud login. Reported: manual address in app v3.x | HTTP: `GET /shelly`, `GET /rpc/EM.GetStatus?id=0`, `GET /status` fallback | 80 | — | Low: one add-on's README (Solarbank 2 E1600 AC); Anker's guide offers only the cloud path |
| **Jackery** SolarVault 3 | Unknown; a forum reply suggests a Shelly account may be asked for | Unknown | — | — | Low: listed as supported by another emulator, nothing more |
| **Zendure** SolarFlow / Hyper / Hub | **Shelly Cloud login only.** Local operation continues after pairing a *real* Shelly | — | — | — | High that emulators cannot pair |
| **EcoFlow** STREAM / PowerStream | **Shelly Cloud only**; control goes through EcoFlow's cloud | — | — | — | High that emulators cannot pair |
| **Marstek** B2500, Venus, Jupiter | Address typed in, or UDP discovery | UDP JSON-RPC, ports 1010 / 2220 | — | Two-decimal floats (B2500) | Covered by the existing UDP emulation |

Evidence notes:

- **Port 80.** Another emulator's documentation: "Some consumers have the
  target port hardcoded to `80` … Known examples are the Growatt Noah/Nexa 2000
  and Hoymiles storages." Forum excerpts say the same of Hoymiles ("ignores the
  port announced over mDNS").
- **Hoymiles `EM.GetConfig`.** A February 2025 fix titled "EM.GetConfig needed
  by latest hoymiles firmware" added exactly `id, name (null),
  blink_mode_selector, phase_selector, monitor_phase_sequence, reverse,
  ct_type`. Ours returns the same fields.
- **Growatt cloud state.** An August 2025 fix, "Growatt NOAH needs disabled
  cloud settings to work in local mode", changed the reported cloud status to
  `enabled: false, connected: false`. Ours already reports that.
- **Solakon naming.** The February 2026 fix changed the instance and SRV host
  from `shellypro3em-<mac>` to `ShellyPro3EM-<MAC>`, set the SRV host
  explicitly (it had been the machine's own name), lowercased TXT `id` and
  colon-formatted TXT `mac`. We announce exactly that.
- **`Content-Length`.** Reported necessary for emulated meters found by
  discovery. `shelly/tcp_server_test.py` pins it.
- **Zendure.** The Zendure app adds the meter from a Shelly Cloud account; users
  in the Zendure forum could keep a real Shelly working locally after pairing
  (password off, cloud off), never pair without the cloud.

## What turned out not to matter

Each of these was an open question; the evidence says batteries do not gate on
them, so changing them buys nothing and risks the one combination with field
reports behind it.

- **TXT `gen`.** Emulators reported working with Hoymiles and Growatt send `gen`
  empty (Tasmota never sets it), `2` (Energy2Shelly), and `2` then `3` (the Java
  emulator, which switched its default in January 2026 without a recorded
  reason). We send `2`, which matches the model we claim to be.
- **Name case.** Working emulators announce `shellypro3em-<mac>`,
  `shellypro3em-<MAC>` and `ShellyPro3EM-<MAC>`. Real firmware, per Shelly's
  knowledge base, uses a lowercase instance (`shellyhtg3-84fce63f8908`) with a
  mixed-case host (`ShellyHTG3-84FCE63F8908.local`). We keep `ShellyPro3EM-<MAC>`
  for both, the only form with a Solakon report behind it.
- **TXT `app`.** Real firmware sends a short code (`HTG3`, `S2PMG4`; `Pro3EM` for
  this model, unverified); emulators send `shellypro3em` or nothing. No battery
  is known to read it.
- **`NotifyStatus` pushes.** None of the emulators reported working push
  anything; the batteries poll. Only Home Assistant's own Shelly integration
  would refresh faster with pushes.
- **Methods beyond the meter.** Ottelo's Tasmota script, reported working with
  Hoymiles and Growatt, answers only `EM.GetStatus` and `Shelly.GetStatus` on
  `/rpc/*`. Our wider method set is for other consumers (Home Assistant's
  `aioshelly`, the Shelly app), not the batteries.

## What must not regress

| Behaviour | Who needs it | Where | Pinned by |
|---|---|---|---|
| HTTP on port 80 by default | Hoymiles, Growatt | `ShellySettings.tcp_port` | `ini_config_test.py::test_the_shelly_emulation_answers_on_port_80_by_default` |
| `Content-Length` on every response, no chunking | Discovery-based batteries | `shelly/tcp_server.py` `_json_response` | `tcp_server_test.py::test_every_response_declares_its_length` |
| `Server: ShellyHTTP/1.0.0`, bare `application/json` | Consumers comparing headers | `shelly/tcp_server.py` | `tcp_server_test.py` |
| `EM.GetConfig` field set | Hoymiles | `shelly/rpc.py` `em_get_config` | `rpc_test.py` |
| Cloud reported disabled | Growatt | `shelly/rpc.py` cloud builders | `rpc_test.py` |
| Instance/SRV host `ShellyPro3EM-<MAC>`, TXT `id` lowercase, TXT `mac` `AA:BB:…` | Solakon | `shelly/identity.py`, `mdns.py` | `mdns_test.py`, `identity_test.py` |
| Two-decimal floats | Marstek B2500 (UDP) | `shelly/wire.py` | `wire_test.py` |
| Unknown methods answered with a JSON-RPC 404 error, never silence | Probing clients | `shelly/tcp_server.py` | `tcp_server_test.py` |

## Several batteries on one AstraMeter

Two separate problems, often confused.

### 1. Identity: a battery that insists on its own meter

AstraMeter presents **one** Shelly identity — one MAC, one device ID — to every
client. Indevolt's guide states that one Shelly Pro 3EM pairs with a single
Indevolt system, so a second Indevolt system cannot pair with an AstraMeter the
first one already uses. Whether other brands bind exclusively is unknown;
Hoymiles firmware can instead join two MS-A2 into one system that shares one
meter, which avoids the question for them.

**How the other emulator does it.** Since March 2025 ("Support of multiple
storages (shared load)") it takes a list of client contexts:

```hocon
client-contexts = [
  { address = "192.168.178.30", mac = "bcdef01234567", power-factor = 0.3 },
  { address = "192.168.178.70", mac = "cdef012345678", power-factor = 0.7 }
]
default-client-power-factor = 1.0
```

A request is matched by its **source IP**. For a listed client, the MAC is
replaced and the device name becomes `shellypro3em-<mac in lowercase>` in:
the JSON-RPC envelope's `src`, `Shelly.GetDeviceInfo` (`id`, `mac`),
`Sys.GetConfig.device` (`name`, `mac`) and `Sys.GetStatus.mac`. mDNS still
announces only the default identity — per-client identities are reachable by
address, not discoverable.

Matching by source IP is the only workable key: every identity would resolve to
the same address and port (Hoymiles and Growatt insist on 80), and batteries
connect by address, so neither the port nor a `Host` header can tell them
apart.

**Design for AstraMeter.**

- *Configuration.* One option in `[EMULATOR_SHELLYPRO3EM]` listing client
  addresses, each with an optional MAC:
  `CLIENTS = 192.168.1.30, 192.168.1.31=AABBCCDDEE01`. An address without a MAC
  gets one derived from the base MAC and that address (a hash with the
  locally-administered bit set), so it is stable across restarts and does not
  depend on list order. A new option must reach every surface: follow the
  `add-config-option` skill.
- *Identity.* Extend `shelly/identity.py` with a function that maps a client
  address to a `ShellyIdentity`. Only the base identity needs persisting; the
  derived ones are a pure function of it. Keep the `shellypro3em-<mac>` lowercase
  name form for the derived identities' `id` — it is what a user types.
- *Serving.* All the RPC builders already read the identity from
  `rpc.RequestContext`, so nothing in `shelly/rpc.py` changes. In
  `shelly/tcp_server.py`, `_context()` becomes `_context(request)` and picks the
  identity by `_client_ip(request)`; the five places that set the envelope's
  `src` from `self._identity` take it from that context instead.
- *mDNS.* Keep announcing only the base identity, as the other emulator does.
  Announcing every identity would list several "meters" in each battery's
  discovery list with no way to tell which is whose.
- *Dashboard.* The status snapshot (`ShellySnapshot`, `status/serialize.py`,
  `web/ts/dashboard/`) must list each configured client with its device ID:
  Indevolt users type that ID into their app, and today they read the single
  one from *Identifies as*.
- *Tests.* Two clients from different addresses get different `id`/`mac` in
  every surface listed above; an unlisted client gets the base identity; a
  derived MAC survives a restart; the envelope `src` follows the client.

Open before implementing: whether Indevolt's binding is enforced by the battery
or by Indevolt's cloud (if the cloud, the ID alone may not be enough), and
whether any discovery-based battery binds exclusively (if so, it would need a
*discoverable* second identity, which this design does not give it).

### 2. Load: two batteries both correcting the same grid reading

Two independent batteries that each read the full grid power each try to
correct all of it, so together they overshoot — and, over time, one ends up
charging the other. The other emulator's answer is the per-client
`power-factor` above (each battery sees a fixed share of the reading); its own
documentation calls the feature experimental and admits the drift, suggesting
a cron job that switches the emulator off briefly to reset it.

For AstraMeter this is a different feature from the identity problem, and not
recommended as a copy: a fixed share is open-loop, whereas AstraMeter already
coordinates several batteries properly where the protocol allows it (the CT002
balancer). For Hoymiles the battery-side answer — joining the units into one
system — is better than either. Implement shares only if a user with two
independent Shelly-reading batteries asks, and then as a per-client factor on
the reading in `Shelly._read_for_http`, keyed by the same client list as the
identity.
