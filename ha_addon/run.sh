#!/usr/bin/with-contenv bash
# AstraMeter reads the add-on configuration itself: `--addon` makes it load the
# user's options from /data/options.json and query the Supervisor API for the
# MQTT broker, the add-on slug and Home Assistant readiness (see
# src/astrameter/config/addon.py). All this script has to do is hand over with
# the container environment in place, so SUPERVISOR_TOKEN reaches the app.
exec /app/.venv/bin/astrameter --addon
