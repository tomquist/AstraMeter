---
description: Works on the emulated Shelly Pro 3EM's HTTP surface and mDNS announcement — what each battery that reads it needs, what must not regress, how to verify it (including the Docker image in a sandbox), and the design for giving several batteries a meter each. Use when changing anything under src/astrameter/shelly/ or src/astrameter/mdns.py, adding a battery to the compatibility table, or when a battery that discovers its meter won't pair.
---

# Shelly emulation

The UDP emulation serves batteries that are *told* the meter's address
(Marstek). The HTTP surface and the mDNS announcement serve those that *find*
it — Hoymiles, Growatt, Solakon, Indevolt. User-facing: `docs/shelly-tcp.md`.
Everything known about those batteries, with sources, what turned out not to
matter, and the multi-battery design: `docs/shelly-battery-research.md`. Read
that before changing a response shape, a default, or the announced names —
several of them exist because one specific battery needed them, and nothing in
the code says which.

## Where things are

| Concern | File |
|---|---|
| Method builders, response shapes, profiles | `src/astrameter/shelly/rpc.py` |
| Routes, transports (GET / POST / WebSocket), headers | `src/astrameter/shelly/tcp_server.py` |
| JSON with two-decimal floats | `src/astrameter/shelly/wire.py` |
| MAC and the three names, persisted across restarts | `src/astrameter/shelly/identity.py` |
| mDNS registration and refresh | `src/astrameter/mdns.py` |
| Announced address, interface discovery | `src/astrameter/net_info.py` |
| Wiring into the emulator, per-client liveness | `src/astrameter/shelly/shelly.py` |
| Which device owns the HTTP surface | `src/astrameter/main.py` (`tcp_owner_index`) |
| Port 80 without root in the image | `Dockerfile`, `docker-entrypoint.sh` |

Only the `shellypro3em` family serves HTTP, and of a `shellypro3em` pair only
one device owns it. The HTTP identity (MAC-derived) is deliberately separate
from `DEVICE_IDS`, which names MQTT topics and Home Assistant entities — do not
merge them.

## What must not regress

The table under "What must not regress" in the research notes maps each
battery-critical behaviour to its test. The short list: port 80 by default,
`Content-Length` on every response, `Server: ShellyHTTP/1.0.0` with a bare
`application/json`, the `EM.GetConfig` field set, cloud reported disabled, the
`ShellyPro3EM-<MAC>` names with a lowercase TXT `id`, and two-decimal floats.
A new battery requirement gets a row there and a test here, in the same change.

## Verify

```bash
uv run pytest src/astrameter/shelly/ src/astrameter/mdns_test.py \
  src/astrameter/net_info_test.py src/astrameter/config/ini_config_test.py \
  tests/test_shelly_tcp_integration.py
```

`tests/test_docker_image.py` skips without a built image; CI builds one in the
`docker-image` job. In a sandbox behind the agent proxy the plain build fails
(the proxy refuses plain-HTTP apt and its CA is not in the image), so build a
throwaway copy of the Dockerfile — never edit the real one for this:

```bash
dockerd >/tmp/dockerd.log 2>&1 &            # if `docker info` fails
mkdir -p /tmp/ca && cp /root/.ccr/ca-bundle.crt /tmp/ca/
awk '{print} /^FROM python:3.12-slim/ {
  print "COPY --from=ca ca-bundle.crt /etc/ssl/certs/proxy-ca.crt"
  print "ENV SSL_CERT_FILE=/etc/ssl/certs/proxy-ca.crt PIP_CERT=/etc/ssl/certs/proxy-ca.crt"
  print "RUN sed -i \"s|http://deb.debian.org|https://deb.debian.org|g\" /etc/apt/sources.list.d/debian.sources && echo \"Acquire::https::CAInfo \\\"/etc/ssl/certs/proxy-ca.crt\\\";\" > /etc/apt/apt.conf.d/99proxyca"
}' Dockerfile > /tmp/Dockerfile.sandbox
docker buildx build --network host --load -t astrameter:test \
  -f /tmp/Dockerfile.sandbox --build-context ca=/tmp/ca \
  --build-arg HTTPS_PROXY="$HTTPS_PROXY" --build-arg https_proxy="$HTTPS_PROXY" .
uv run pytest tests/test_docker_image.py -v
```

Only `--network host` tests the port-80 capability: Docker's bridge network
lets any user bind low ports, so a bridge test passes whatever the image does.

To see the surface as a client would, run the app with `TCP_PORT` above 1024
and `curl -i` a few paths; for Home Assistant's view, drive it with
`aioshelly`'s `RpcDevice` (`pip install aioshelly` in a scratch venv).

## Several batteries, one meter each

Not implemented. Indevolt pairs one meter with one system, so a second Indevolt
cannot share an AstraMeter. The research notes carry the full design —
identities keyed by client address, configured in `[EMULATOR_SHELLYPRO3EM]`,
chosen in `ShellyTcpServer._context`, listed on the dashboard — and the two
questions to answer on hardware first. The option it adds goes through the
`add-config-option` skill.
