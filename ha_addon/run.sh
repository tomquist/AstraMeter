#!/usr/bin/with-contenv bashio
set -e

# Redact secrets from everything this script and its children log. bashio
# echoes full supervisor API responses at debug level — including the MQTT
# broker password (GET /services/mqtt) and the add-on's own option values such
# as the Marstek account password/email (GET /addons/self/info) — which would
# otherwise end up verbatim in shared debug logs (see discussion #520). Route
# all output through a line-buffered filter so those values never reach the log;
# the Python app inherits the same filtered stdout, so it is covered too.
read -r -d '' __ASTRAMETER_REDACT_PY <<'PYEOF' || true
import re, sys

# Stay alive on non-UTF-8 bytes: this filter is in the critical log path, so a
# single undecodable byte must not crash it and break SIGPIPE-sensitive writers.
try:
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

PATTERNS = [
    # JSON: "<...password|token|secret|username|mailbox...>": "<value>"
    (re.compile(
        r'("[A-Za-z0-9_]*'
        r'(?:password|passwd|secret|token|api[_-]?key|username|mailbox)"'
        r'\s*:\s*")[^"]*"', re.I), r'\1REDACTED"'),
    # URI userinfo: scheme://user:pass@host
    (re.compile(r'([a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s:]+:[^/@\s]+@'),
     r'\1***:***@'),
]

for line in iter(sys.stdin.readline, ''):
    for pat, repl in PATTERNS:
        line = pat.sub(repl, line)
    sys.stdout.write(line)
    sys.stdout.flush()
PYEOF

if [ -z "${ASTRAMETER_NO_LOG_REDACT:-}" ] && command -v python3 >/dev/null 2>&1; then
    export ASTRAMETER_NO_LOG_REDACT=1
    exec > >(python3 -c "$__ASTRAMETER_REDACT_PY") 2>&1
fi

# Function to check if Home Assistant is ready
wait_for_homeassistant() {
    local max_attempts=60  # 5 minutes with 5-second intervals
    local attempt=1
    local ha_url="http://supervisor:80/core/api/"
    
    bashio::log.info "Waiting for Home Assistant to be ready..."
    
    while [ $attempt -le $max_attempts ]; do
        bashio::log.debug "Checking Home Assistant readiness (attempt $attempt/$max_attempts)..."
        
        # Check if the API responds with a valid status
        if curl -s -f -H "Authorization: Bearer $SUPERVISOR_TOKEN" \
           -H "Content-Type: application/json" \
           --connect-timeout 5 --max-time 10 \
           "$ha_url" > /dev/null 2>&1; then
            bashio::log.info "Home Assistant is ready! Proceeding with AstraMeter startup..."
            return 0
        fi
        
        bashio::log.debug "Home Assistant not ready yet, waiting 5 seconds..."
        sleep 5
        attempt=$((attempt + 1))
    done
    
    bashio::log.warning "Home Assistant may not be fully ready after $((max_attempts * 5)) seconds, but continuing anyway..."
    return 1
}

CONFIG="/app/config.ini"

print_redacted_config() {
    sed -E \
        -e 's/^((MAILBOX|USERNAME|PASSWORD|ACCESSTOKEN|TOKEN|SECRET))\s*=\s*.*/\1 = REDACTED/i' \
        -e 's#^(URI\s*=\s*[a-zA-Z][a-zA-Z0-9+.-]*://)[^/@[:space:]]+@#\1***:***@#i' \
        "$1"
}

# Check if custom config is provided
if bashio::config.has_value 'custom_config' && [ -f "/config/$(bashio::config 'custom_config')" ]; then
    bashio::log.info "Using custom config file: $(bashio::config 'custom_config')"
    if bashio::config.has_value 'marstek_mailbox' || bashio::config.has_value 'marstek_password' || bashio::config.has_value 'marstek_auto_register_ct_device'; then
        bashio::log.warning "App UI Marstek settings are ignored when custom_config is used; values from custom config file take precedence"
    fi
    if bashio::config.has_value 'mqtt_uri'; then
        bashio::log.warning "App UI mqtt_uri is ignored when custom_config is used; the custom config file controls MQTT settings"
    fi
    cp "/config/$(bashio::config 'custom_config')" "$CONFIG"
else
    device_types="$(bashio::config 'device_types')"
    has_ct002=0
    has_ct003=0
    if echo "$device_types" | grep -qi 'ct002'; then
        has_ct002=1
    fi
    if echo "$device_types" | grep -qi 'ct003'; then
        has_ct003=1
    fi

    ct_section="CT002"
    if [ "$has_ct003" -eq 1 ] && [ "$has_ct002" -eq 0 ]; then
        ct_section="CT003"
    fi

    ct_mac=""
    if bashio::config.has_value 'ct_mac'; then
        ct_mac="$(bashio::config 'ct_mac')"
    fi

    min_efficient_power=""
    if bashio::config.has_value 'min_efficient_power'; then
        min_efficient_power="$(bashio::config 'min_efficient_power')"
    fi

    efficiency_rotation_interval=""
    if bashio::config.has_value 'efficiency_rotation_interval'; then
        efficiency_rotation_interval="$(bashio::config 'efficiency_rotation_interval')"
    fi

    min_dc_output=""
    if bashio::config.has_value 'min_dc_output'; then
        min_dc_output="$(bashio::config 'min_dc_output')"
    fi

    active_control=""
    if bashio::config.has_value 'active_control'; then
        active_control="$(bashio::config 'active_control')"
    fi

    grid_predict_trust=""
    if bashio::config.has_value 'grid_predict_trust'; then
        grid_predict_trust="$(bashio::config 'grid_predict_trust')"
    fi

    # Opt-in HTTP cloud reporting (hamedata.com). Only emit keys the user set.
    cloud_reporting=""
    if bashio::config.has_value 'cloud_reporting'; then
        cloud_reporting="$(bashio::config 'cloud_reporting')"
    fi
    cloud_reporting_host=""
    if bashio::config.has_value 'cloud_reporting_host'; then
        cloud_reporting_host="$(bashio::config 'cloud_reporting_host')"
    fi
    cloud_reporting_interval=""
    if bashio::config.has_value 'cloud_reporting_interval'; then
        cloud_reporting_interval="$(bashio::config 'cloud_reporting_interval')"
    fi

    # Emit the cloud-reporting keys (if any) into the current [CT00x] section.
    # The trailing `return 0` is required: the last `[ -n ... ] && echo` is a
    # no-op when the key is unset (the common case), which would make the
    # function return 1. Under `set -e` a bare call to a function returning
    # non-zero aborts the whole script — silently, since we're inside the
    # `{ ... } > "$CONFIG"` block — so the add-on never started (issue #510).
    emit_cloud_reporting() {
        [ -n "$cloud_reporting" ] && echo "CLOUD_REPORTING=$cloud_reporting"
        [ -n "$cloud_reporting_host" ] && echo "CLOUD_REPORTING_HOST=$cloud_reporting_host"
        [ -n "$cloud_reporting_interval" ] && echo "CLOUD_REPORTING_INTERVAL=$cloud_reporting_interval"
        return 0
    }

    # Emit the optional CT002/CT003 balancer / active-control tuning knobs (only
    # the ones the user actually set) into the current [CT00x] section. Mirrors
    # the "balancer" group of the web config editor; main.py reads each key from
    # the section with a built-in default, so an unset option simply keeps that
    # default. The trailing `return 0` guards `set -e` the same way
    # emit_cloud_reporting does (see the note above).
    emit_balancer_options() {
        local pair opt key
        for pair in \
            "fair_distribution:FAIR_DISTRIBUTION" \
            "balance_gain:BALANCE_GAIN" \
            "balance_deadband:BALANCE_DEADBAND" \
            "max_correction_per_step:MAX_CORRECTION_PER_STEP" \
            "error_boost_threshold:ERROR_BOOST_THRESHOLD" \
            "error_boost_max:ERROR_BOOST_MAX" \
            "error_reduce_threshold:ERROR_REDUCE_THRESHOLD" \
            "max_target_step:MAX_TARGET_STEP" \
            "pace_base_step:PACE_BASE_STEP" \
            "pace_max_step:PACE_MAX_STEP" \
            "osc_damp_max:OSC_DAMP_MAX" \
            "osc_damp_alpha:OSC_DAMP_ALPHA" \
            "osc_damp_decay:OSC_DAMP_DECAY" \
            "osc_damp_threshold:OSC_DAMP_THRESHOLD" \
            "concentrate_deadband:CONCENTRATE_DEADBAND" \
            "import_trim_w:IMPORT_TRIM_W"; do
            opt="${pair%%:*}"
            key="${pair##*:}"
            if bashio::config.has_value "$opt"; then
                echo "${key}=$(bashio::config "$opt")"
            fi
        done
        return 0
    }

    # Generate default config
    {
        echo "[GENERAL]"
        echo "DEVICE_TYPE=$(bashio::config 'device_types')"
        echo "THROTTLE_INTERVAL=$(bashio::config 'throttle_interval')"
        if bashio::config.has_value 'dedupe_time_window'; then
            echo "DEDUPE_TIME_WINDOW=$(bashio::config 'dedupe_time_window')"
        fi
        echo "ENABLE_WEB_SERVER=true"
        echo ""
        if [ "$has_ct002" -eq 1 ] && [ "$has_ct003" -eq 1 ]; then
            echo "[CT002]"
            echo "CT_MAC=$ct_mac"
            [ -n "$active_control" ] && echo "ACTIVE_CONTROL=$active_control"
            [ -n "$min_efficient_power" ] && echo "MIN_EFFICIENT_POWER=$min_efficient_power"
            [ -n "$efficiency_rotation_interval" ] && echo "EFFICIENCY_ROTATION_INTERVAL=$efficiency_rotation_interval"
            [ -n "$min_dc_output" ] && echo "MIN_DC_OUTPUT=$min_dc_output"
            [ -n "$grid_predict_trust" ] && echo "GRID_PREDICT_TRUST=$grid_predict_trust"
            emit_balancer_options
            emit_cloud_reporting
            echo ""
            echo "[CT003]"
            echo "CT_MAC=$ct_mac"
            [ -n "$active_control" ] && echo "ACTIVE_CONTROL=$active_control"
            [ -n "$min_efficient_power" ] && echo "MIN_EFFICIENT_POWER=$min_efficient_power"
            [ -n "$efficiency_rotation_interval" ] && echo "EFFICIENCY_ROTATION_INTERVAL=$efficiency_rotation_interval"
            [ -n "$min_dc_output" ] && echo "MIN_DC_OUTPUT=$min_dc_output"
            [ -n "$grid_predict_trust" ] && echo "GRID_PREDICT_TRUST=$grid_predict_trust"
            emit_balancer_options
            emit_cloud_reporting
            echo ""
        else
            echo "[$ct_section]"
            echo "CT_MAC=$ct_mac"
            [ -n "$active_control" ] && echo "ACTIVE_CONTROL=$active_control"
            [ -n "$min_efficient_power" ] && echo "MIN_EFFICIENT_POWER=$min_efficient_power"
            [ -n "$efficiency_rotation_interval" ] && echo "EFFICIENCY_ROTATION_INTERVAL=$efficiency_rotation_interval"
            [ -n "$min_dc_output" ] && echo "MIN_DC_OUTPUT=$min_dc_output"
            [ -n "$grid_predict_trust" ] && echo "GRID_PREDICT_TRUST=$grid_predict_trust"
            emit_balancer_options
            emit_cloud_reporting
            echo ""
        fi

        marstek_auto_register_ct_device="false"
        if bashio::config.has_value 'marstek_auto_register_ct_device'; then
            marstek_auto_register_ct_device="$(bashio::config 'marstek_auto_register_ct_device')"
        fi

        if [ "$marstek_auto_register_ct_device" = "true" ] && bashio::config.has_value 'marstek_mailbox' && bashio::config.has_value 'marstek_password'; then
            echo "[MARSTEK]"
            echo "ENABLE=True"
            echo "BASE_URL=https://eu.hamedata.com"
            echo "MAILBOX=$(bashio::config 'marstek_mailbox')"
            echo "PASSWORD=$(bashio::config 'marstek_password')"
            echo "TIMEZONE=Europe/Berlin"
            echo ""
        fi

        echo "[HOMEASSISTANT]"
        echo "IP=supervisor"
        echo "PORT=80"
        echo "API_PATH_PREFIX=/core"
        echo "WAIT_FOR_NEXT_MESSAGE=$(bashio::config 'wait_for_next_message')"
        if bashio::config.has_value 'power_output_alias'; then
            echo "POWER_CALCULATE=True"
            echo "POWER_INPUT_ALIAS=$(bashio::config 'power_input_alias')"
            echo "POWER_OUTPUT_ALIAS=$(bashio::config 'power_output_alias')"
        else
            echo "POWER_CALCULATE=False"
            echo "CURRENT_POWER_ENTITY=$(bashio::config 'power_input_alias')"
        fi
        if bashio::config.has_value 'power_offset'; then
            power_offset="$(bashio::config 'power_offset' | tr -d '\r\n')"
            echo "POWER_OFFSET=$power_offset"
        fi
        if bashio::config.has_value 'power_multiplier'; then
            power_multiplier="$(bashio::config 'power_multiplier' | tr -d '\r\n')"
            echo "POWER_MULTIPLIER=$power_multiplier"
        fi
        if bashio::config.has_value 'smooth_target_alpha'; then
            echo "SMOOTH_TARGET_ALPHA=$(bashio::config 'smooth_target_alpha')"
        fi
        if bashio::config.has_value 'max_smooth_step'; then
            echo "MAX_SMOOTH_STEP=$(bashio::config 'max_smooth_step')"
        fi
        if bashio::config.has_value 'deadband'; then
            echo "DEADBAND=$(bashio::config 'deadband')"
        fi
        if bashio::config.has_value 'hampel_window'; then
            echo "HAMPEL_WINDOW=$(bashio::config 'hampel_window')"
        fi
        if bashio::config.has_value 'hampel_n_sigma'; then
            echo "HAMPEL_N_SIGMA=$(bashio::config 'hampel_n_sigma')"
        fi
        if bashio::config.has_value 'hampel_min_threshold'; then
            echo "HAMPEL_MIN_THRESHOLD=$(bashio::config 'hampel_min_threshold')"
        fi
        if bashio::config.has_value 'pid_kp'; then
            echo "PID_KP=$(bashio::config 'pid_kp')"
        fi
        if bashio::config.has_value 'pid_ki'; then
            echo "PID_KI=$(bashio::config 'pid_ki')"
        fi
        if bashio::config.has_value 'pid_kd'; then
            echo "PID_KD=$(bashio::config 'pid_kd')"
        fi
        if bashio::config.has_value 'pid_output_max'; then
            echo "PID_OUTPUT_MAX=$(bashio::config 'pid_output_max')"
        fi
        if bashio::config.has_value 'pid_mode'; then
            echo "PID_MODE=$(bashio::config 'pid_mode')"
        fi

        # Fetch this add-on's slug from the supervisor so MQTT discovery can
        # link discovered meter devices to the add-on device via_device.
        addon_slug=""
        addon_info_json=""
        if addon_info_json="$(bashio::api.supervisor GET '/addons/self/info' false)" && [ -n "$addon_info_json" ]; then
            addon_slug="$(echo "$addon_info_json" | jq -r '.slug // empty')"
        fi
        if [ -n "$addon_slug" ]; then
            bashio::log.info "Resolved add-on slug for HA discovery: $addon_slug"
        else
            bashio::log.warning "Failed to resolve add-on slug from supervisor; meter devices will not be linked via_device"
            addon_slug=""
        fi

        if bashio::config.has_value 'mqtt_uri'; then
            bashio::log.info "Using custom MQTT broker URL from configuration"
            echo ""
            echo "[MQTT_INSIGHTS]"
            echo "URI=$(bashio::config 'mqtt_uri')"
            echo "HA_DISCOVERY=True"
            [ -n "$addon_slug" ] && echo "ADDON_SLUG=$addon_slug"
        elif bashio::services.available "mqtt"; then
            bashio::log.info "Using Home Assistant's internal MQTT broker"
            echo ""
            echo "[MQTT_INSIGHTS]"
            echo "BROKER=$(bashio::services 'mqtt' 'host')"
            echo "PORT=$(bashio::services 'mqtt' 'port')"
            echo "USERNAME=$(bashio::services 'mqtt' 'username')"
            echo "PASSWORD=$(bashio::services 'mqtt' 'password')"
            echo "TLS=$(bashio::services 'mqtt' 'ssl')"
            echo "HA_DISCOVERY=True"
            [ -n "$addon_slug" ] && echo "ADDON_SLUG=$addon_slug"
        fi
    } > "$CONFIG"
fi

print_redacted_config "$CONFIG"

# Wait for Home Assistant to be ready before starting. It returns non-zero on
# timeout (and logs a warning) but we continue anyway, so guard the bare call
# from `set -e`.
wait_for_homeassistant || true

. /app/.venv/bin/activate
cd /app

# Get log level from configuration (defaults to info)
LOG_LEVEL=$(bashio::config 'log_level')
bashio::log.info "Starting AstraMeter with log level: $LOG_LEVEL"
astrameter --loglevel "$LOG_LEVEL"