# AstraMeter deployment

Stand: 11. Oktober 2026

This deployment runs AstraMeter 2.3.1 on a dedicated ESP32 and presents one
shared CT002 to two Marstek Venus E batteries. The Shelly Pro 3EM is the only
grid-power source used by the control loop.

## Devices

| Device | Address / identifier | Role |
|---|---|---|
| ESP32-D0WDQ6, 4 MB flash | DHCP: `10.0.40.74`; USB observed as `COM7` | AstraMeter CT002 emulator and dashboard |
| Shelly Pro 3EM | `10.0.40.72` | Three-phase whole-house grid measurement |
| Marstek Venus E 2.0 | `10.0.40.85`; CT consumer `009b08782ad6` | Controlled AC battery |
| Marstek Venus E 3.0 | `10.0.40.77`; CT consumer `0c587b17db18` | Controlled AC battery |
| FoxESS cloud connector | `10.0.40.69` | Not directly controlled by AstraMeter |

The registered shared CT is an HME-4/CT002 with MAC `02b25069325f`. Both
Marstek batteries are configured in automatic mode and poll this CT. The app
may display only one battery in `Venus-Group-325f` because V2 and V3 are
different generations; AstraMeter independently confirmed both consumers.

## Verified state

- ESPHome validation and compilation succeeded for board profile `esp32dev`.
- The detected ESP32 flash size is 4 MB.
- The production firmware uses about 52% of its application partition.
- The dashboard is available at `http://10.0.40.74/` while that DHCP lease is
  unchanged and is intentionally read-only.
- The Shelly endpoint `/rpc/EM.GetStatus?id=0` is read every second and all
  three phase values are received.
- Both batteries poll approximately every 0.5-0.7 seconds and participate in
  balancing. Two consumers and a stable control verdict were observed.
- One-time Marstek cloud registration succeeded. Cloud credentials are not
  referenced by the final YAML and are not compiled into the final firmware.

## Control behavior

The Shelly measures the complete grid connection, including FoxESS and both
Marstek batteries. AstraMeter therefore controls only the residual grid flow;
FoxESS power must not be added to the Shelly reading.

At demand below the configured `150 W` efficiency threshold, AstraMeter may
intentionally run only one Marstek battery. At higher demand it can distribute
the target across both batteries. Saturation detection is enabled for a battery
that is empty, full, or otherwise unable to follow its target.

At the time of commissioning the V2 was at 11% and the V3 at 15%, while FoxESS
was supplying nearly all house demand. Only residual fluctuations of roughly
-40 W to +50 W were observed, so a meaningful discharge/load-sharing test was
deferred until both Marstek batteries have charged to at least 25-30%.

## Files and maintenance

- `astrameter-ct002.yaml` is the deployed configuration.
- `secrets.yaml` contains local credentials and is ignored by Git.
- `secrets.yaml.example` documents the required Wi-Fi keys without credentials.

Validate, compile, and update over the network:

```powershell
uv tool run --from esphome esphome config astrameter-ct002.yaml
uv tool run --from esphome esphome compile astrameter-ct002.yaml
uv tool run --from esphome esphome upload astrameter-ct002.yaml --device 10.0.40.74
```

If the DHCP address changes, use `astrameter-ct002.local` or the new address.
Use USB only when OTA is unavailable; verify the current COM port first.

## Remaining checks

1. With both Marstek batteries above 25-30%, apply a controlled 500-1000 W
   load and confirm that both react without oscillation.
2. Confirm over a full charge/discharge cycle that grid import is positive and
   export is negative.
3. Watch for material FoxESS overcompensation that could charge a Marstek
   battery from FoxESS. If needed, introduce a small import safety margin only
   after measuring the behavior.
4. Reserve the ESP32 address in DHCP if a stable dashboard address is wanted.

Do not connect the photographed 5 V MAX485 module directly to ESP32 GPIOs. The
FoxESS RS485/EW11 path is outside this deployment and remains unconfigured.
