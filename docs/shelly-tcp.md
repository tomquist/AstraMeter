# Shelly discovery and the HTTP surface

Most batteries that speak Shelly are told the meter's address by hand and poll
it over UDP. Some don't: they look for a Shelly on the network, find one, and
then talk to it over HTTP. This page is about that second kind.

With a `shellypro3em` device type, AstraMeter announces itself on your network
and answers the HTTP requests those batteries make. Both are on by default and
neither needs configuring, so if your battery discovers meters by itself, it
should find AstraMeter without you doing anything.

## What it announces

AstraMeter advertises two mDNS services, which is what a Shelly does:

| | |
|---|---|
| Service types | `_shelly._tcp.local.`, `_http._tcp.local.` |
| Service name | `ShellyPro3EM-<MAC>` |
| Hostname | `ShellyPro3EM-<MAC>.local.` |
| Port | the HTTP port below |
| Discovery fields | `id`, `mac`, `gen=2`, `app=shellypro3em`, `model=SPEM-003CEBEU`, `arch=esp32`, `fw_id`, `ver` |

`<MAC>` is the emulated MAC — see [Identity](#identity) — and `id` is the same
MAC in lowercase, which is also what the HTTP surface reports as its own `id`.

You can check what your network sees with any mDNS browser:

```bash
avahi-browse -r _shelly._tcp
```

## What it serves

On the HTTP port, AstraMeter answers the request shapes a Shelly Pro 3EM
answers:

- `GET /rpc/<Method>` — one request per method, parameters in the query string
- `POST /rpc` — a JSON-RPC frame, answered with the full envelope
- `POST /rpc/<Method>` — the parameters as the body, answered with the bare result
- `GET /rpc` upgraded to a **WebSocket** — the transport Home Assistant's Shelly
  integration uses
- `GET /shelly`, `GET /settings` — device identity
- `GET /status`, `GET /emeter/0..2` — the older single-page endpoints, which some
  batteries probe to confirm what they are talking to

The meter reading comes from whichever power source's `NETMASK` covers the
caller, exactly as on the UDP path.

## Configuration

Everything here lives in `[EMULATOR_SHELLYPRO3EM]`, or under the matching
`shelly_*` options in the Home Assistant add-on.

> The section is called `EMULATOR_SHELLYPRO3EM`, not `SHELLYPRO3EM`. Sections
> beginning with `SHELLY` describe a Shelly *power meter you read from*, so the
> emulator's own section is prefixed to keep the two apart.

| Key | Add-on option | Default | What it does |
|---|---|---|---|
| `TCP_PORT` | `shelly_tcp_port` | `80` | Port the HTTP surface answers on. `-1` serves no HTTP but still announces the meter. |
| `MDNS_ENABLED` | `shelly_mdns_enabled` | `True` | Announce the meter on your network. |
| `SERVE_GEN1_ENDPOINTS` | `shelly_serve_gen1_endpoints` | `True` | Also answer `/status` and `/emeter/N`. |
| `MAC` | `shelly_mac` | derived | The MAC the meter identifies itself by. |
| `MDNS_HOST` | `shelly_mdns_host` | detected | Address to announce, if not the one this machine routes from. Takes an IPv4 address or an interface name. |
| `HOSTNAME` | `shelly_hostname` | derived | Pins the announced hostname, including its capitalisation. |
| `MDNS_INSTANCE` | `shelly_mdns_instance` | derived | Pins the announced service name, independently of the hostname. |
| `MDNS_TXT` | `shelly_mdns_txt` | — | Extra `key=value` discovery fields, comma separated. A value containing a comma cannot be expressed. |

### Port 80

Several battery models have port 80 hardcoded and cannot be pointed anywhere
else, which is why it is the default. Ports below 1024 need extra privileges on
Linux, so what you have to do depends on how you run AstraMeter — see the
[installation matrix](#per-installation-notes).

If your battery honours the port in the announcement, `TCP_PORT = 8080` works
and needs no privileges at all. Whether a given model does is worth trying: it
is one setting, and if it works you are done.

## Identity

Batteries recognise the meter by its MAC, so it has to be the same after a
restart. AstraMeter resolves it once, in this order:

1. `MAC`, if you set one
2. the MAC in a device id *you* configured via `DEVICE_IDS`
3. the MAC it saved on a previous start
4. the MAC of the network interface carrying this machine's default route
5. a random locally-administered MAC

Whatever 4 or 5 produced is written to a file, so step 3 answers every later
start even if the interface changes:

| How you run it | Where the identity is kept |
|---|---|
| Home Assistant add-on | `/data/identity.json` |
| Docker or direct, with a config file | `astrameter-identity.json` beside it |
| otherwise | `$ASTRAMETER_STATE_DIR`, else `$XDG_STATE_HOME/astrameter/`, else `~/.local/state/astrameter/` |

If nothing is writable, AstraMeter logs a warning and carries on: step 4 is
already stable for a given machine, so the identity normally survives a restart
anyway. Set `MAC` when it cannot — most often **Docker bridge networking**,
where the container gets a new address on every start.

> This does not change your MQTT or Home Assistant entities. The emulator keeps
> its existing `DEVICE_IDS` identity for those; the MAC here is used only for
> discovery and the HTTP surface.

## Per-installation notes

| | Discovery | Port 80 |
|---|---|---|
| **Home Assistant add-on** | works out of the box | works out of the box (the add-on runs as root on the host network) |
| **Docker, `network_mode: host`** | works | works — the image grants its interpreter the one capability needed to bind it |
| **Docker, bridge networking** | **does not work**; set `MDNS_ENABLED = False` and give the battery the host's address | publish `80:80` |
| **Direct install** | works | needs `CAP_NET_BIND_SERVICE`, root, or a port above 1024 — see below |

Discovery cannot work on Docker bridge networking, and that is not a setting
AstraMeter can fix: a battery's query is sent to a multicast address that a NAT
bridge does not carry, so the announcement never reaches your LAN and the
battery's query never reaches AstraMeter. Use `network_mode: host` if you want
discovery.

For a **direct install** run as a non-root user, grant the capability once:

```bash
sudo setcap cap_net_bind_service=+ep "$(readlink -f "$(which python3)")"
```

or, under systemd, add it to the unit instead of touching the interpreter:

```ini
[Service]
AmbientCapabilities=CAP_NET_BIND_SERVICE
```

If the port cannot be bound, AstraMeter logs one error naming it and carries
on: the UDP emulation and the announcement are unaffected, so batteries that
poll a configured address keep working.

## Home Assistant will discover it too

Because AstraMeter announces itself as a Shelly, Home Assistant's own Shelly
integration will offer it as a discovered device. Adding it is harmless and
gives you the meter's readings as sensors, but it is not how AstraMeter feeds
Home Assistant — that is the dashboard and, if you enable it, MQTT. You can
ignore the discovery notification.

Home Assistant polling the device does **not** make it appear as a battery on
the dashboard: only the meter endpoints a battery actually reads count towards
that.

## If a battery won't pair

1. Check it is discovering anything at all, with `avahi-browse -r _shelly._tcp`
   from another machine on the same network.
2. Check the HTTP surface answers:
   `curl http://<astrameter-host>/shelly` should return a device-info object.
   The dashboard's Shelly card shows the port in use and whether the
   announcement is live.
3. If the battery finds the meter but never connects, it is most likely
   expecting port 80. Confirm `TCP_PORT = 80` and that nothing else on the host
   already holds it.
4. If it still refuses, a battery that checks a specific discovery field can be
   accommodated with `MDNS_TXT` — for example `MDNS_TXT = gen=3`. If you find a
   value that works, please open an issue so it can become the default for that
   model.
