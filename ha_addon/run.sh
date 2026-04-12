#!/usr/bin/with-contenv bashio
set -e

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
        -e 's/^((MAILBOX|PASSWORD|ACCESSTOKEN|TOKEN|SECRET))\s*=\s*.*/\1 = REDACTED/i' \
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

    # Generate default config
    {
        echo "[GENERAL]"
        echo "DEVICE_TYPE=$(bashio::config 'device_types')"
        echo "THROTTLE_INTERVAL=$(bashio::config 'throttle_interval')"
        echo "ENABLE_WEB_SERVER=true"
        echo ""
        if [ "$has_ct002" -eq 1 ] && [ "$has_ct003" -eq 1 ]; then
            echo "[CT002]"
            echo "CT_MAC=$ct_mac"
            [ -n "$min_efficient_power" ] && echo "MIN_EFFICIENT_POWER=$min_efficient_power"
            [ -n "$efficiency_rotation_interval" ] && echo "EFFICIENCY_ROTATION_INTERVAL=$efficiency_rotation_interval"
            echo ""
            echo "[CT003]"
            echo "CT_MAC=$ct_mac"
            [ -n "$min_efficient_power" ] && echo "MIN_EFFICIENT_POWER=$min_efficient_power"
            [ -n "$efficiency_rotation_interval" ] && echo "EFFICIENCY_ROTATION_INTERVAL=$efficiency_rotation_interval"
            echo ""
        else
            echo "[$ct_section]"
            echo "CT_MAC=$ct_mac"
            [ -n "$min_efficient_power" ] && echo "MIN_EFFICIENT_POWER=$min_efficient_power"
            [ -n "$efficiency_rotation_interval" ] && echo "EFFICIENCY_ROTATION_INTERVAL=$efficiency_rotation_interval"
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
        echo "ACCESSTOKEN=$SUPERVISOR_TOKEN"
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

# Wait for Home Assistant to be ready before starting
wait_for_homeassistant

. /app/.venv/bin/activate
cd /app

# Get log level from configuration (defaults to info)
LOG_LEVEL=$(bashio::config 'log_level')
bashio::log.info "Starting AstraMeter with log level: $LOG_LEVEL"
astrameter --loglevel "$LOG_LEVEL"