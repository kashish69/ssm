#!/bin/sh
# Regenerate the mosquitto password file from env on every boot, so the broker
# credentials always match .env / DEVICE_SEEDS with zero manual steps. The
# password file is derived state (source of truth is .env), so rebuilding it
# each start is intentional and idempotent.
set -e

CONFIG_DIR=/mosquitto/config
PASSWD_FILE="$CONFIG_DIR/passwd"

: "${MQTT_SERVICE_USERNAME:=backend-service}"

if [ -z "$MQTT_SERVICE_PASSWORD" ]; then
    echo "entrypoint: MQTT_SERVICE_PASSWORD is empty; set it in .env" >&2
    exit 1
fi

# Backend service account (-c creates/truncates the file).
rm -f "$PASSWD_FILE"
mosquitto_passwd -b -c "$PASSWD_FILE" "$MQTT_SERVICE_USERNAME" "$MQTT_SERVICE_PASSWORD"

# Per-device accounts from DEVICE_SEEDS ("id:name:key;id:name:key;...").
# Each device authenticates with username=device_id, password=api_key.
OLDIFS=$IFS
IFS=';'
# shellcheck disable=SC2086
set -- $DEVICE_SEEDS
IFS=$OLDIFS
for entry in "$@"; do
    [ -z "$entry" ] && continue
    device_id=${entry%%:*}          # before first colon
    rest=${entry#*:}                 # after first colon (name:key)
    api_key=${rest#*:}               # after second colon (name may contain spaces, not colons)
    if [ -n "$device_id" ] && [ -n "$api_key" ] && [ "$device_id" != "$entry" ]; then
        mosquitto_passwd -b "$PASSWD_FILE" "$device_id" "$api_key"
    fi
done

# Make the credential/config files owned + readable only by the mosquitto user
# the broker drops to (fixes the "Unable to open pwfile" + world-readable
# warnings without any host-side chmod).
chown mosquitto:mosquitto "$PASSWD_FILE" "$CONFIG_DIR/acl.conf" "$CONFIG_DIR/mosquitto.conf" 2>/dev/null || true
chmod 0700 "$PASSWD_FILE" "$CONFIG_DIR/acl.conf" "$CONFIG_DIR/mosquitto.conf" 2>/dev/null || true

exec mosquitto -c "$CONFIG_DIR/mosquitto.conf"
