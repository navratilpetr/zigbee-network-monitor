#!/usr/bin/env bash
set -e

echo "=========================================================="
echo " Starting Zigbee Network Monitor..."
echo "=========================================================="

OPTIONS_FILE="/data/options.json"

if [ -f "$OPTIONS_FILE" ]; then
    echo "[Info] Reading configuration from Home Assistant /data/options.json"
    
    export SOURCE_MODE=$(jq -r '.source_mode // "local"' $OPTIONS_FILE)
    export Z2M_PATH=$(jq -r '.z2m_path // "/config/zigbee2mqtt"' $OPTIONS_FILE)
    export SCAN_INTERVAL_MINUTES=$(jq -r '.scan_interval_minutes // 5' $OPTIONS_FILE)
    export OFFLINE_THRESHOLD_HOURS=$(jq -r '.offline_threshold_hours // 12' $OPTIONS_FILE)
    export LQI_WEAK_THRESHOLD=$(jq -r '.lqi_weak_threshold // 50' $OPTIONS_FILE)
    export BATTERY_LOW_THRESHOLD=$(jq -r '.battery_low_threshold // 20' $OPTIONS_FILE)
    export RETENTION_DAYS=$(jq -r '.retention_days // 120' $OPTIONS_FILE)
    
    export MQTT_HOST=$(jq -r '.mqtt_host // "core-mosquitto"' $OPTIONS_FILE)
    export MQTT_PORT=$(jq -r '.mqtt_port // 1883' $OPTIONS_FILE)
    export MQTT_USER=$(jq -r '.mqtt_user // ""' $OPTIONS_FILE)
    export MQTT_PASSWORD=$(jq -r '.mqtt_password // ""' $OPTIONS_FILE)
    export MQTT_TOPIC=$(jq -r '.mqtt_topic // "zigbee2mqtt"' $OPTIONS_FILE)
    
    export GEMINI_API_KEY=$(jq -r '.gemini_api_key // ""' $OPTIONS_FILE)
    export DB_PATH="/data/zigbee_monitor.db"
else
    echo "[Info] Running in standalone/Docker/LXC mode (using environment variables)"
    export DB_PATH="${DB_PATH:-/app/data/zigbee_monitor.db}"
fi

mkdir -p "$(dirname "$DB_PATH")"

echo "[Info] Mode: ${SOURCE_MODE:-local} | Z2M Path: ${Z2M_PATH:-/config/zigbee2mqtt} | DB: $DB_PATH"

exec python3 -m uvicorn app:app --host 0.0.0.0 --port 8765 --workers 1
