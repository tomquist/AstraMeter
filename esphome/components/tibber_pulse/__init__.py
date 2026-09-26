"""ESPHome external component: Tibber Pulse, read locally via the Pulse Bridge.

The ESPHome counterpart of `src/astrameter/powermeter/tibber_pulse.py`: polls
the bridge's telegram endpoint over HTTP Basic auth, decodes the meter's SML
telegram on the ESP and publishes grid power as sensors that `ct002`'s
`power_sensor_l1..l3` can read. Configured as a sensor platform:

    sensor:
      - platform: tibber_pulse
        host: 192.168.1.140
        password: AD56-54BA
        power:
          id: grid_l1

Polling only — the Python source also takes the bridge's WebSocket push
stream, but stock ESPHome has no WebSocket client.
"""

CODEOWNERS = ["@tomquist"]
