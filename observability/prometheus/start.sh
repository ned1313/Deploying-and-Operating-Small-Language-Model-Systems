#!/bin/sh
# Writes the model-host scrape targets from the environment, then starts Prometheus.
# PROMETHEUS_CONFIG=prometheus.secure.yml (module 6) scrapes all three through the TLS gateway; the
# `instance` and `path` labels stay the same so every series continues across the switch.
set -eu
: "${MODEL_HOST:?set MODEL_HOST in observability/.env}"
mkdir -p /tmp/targets
CONFIG="${PROMETHEUS_CONFIG:-prometheus.yml}"

target() {  # file, address, path label
    printf '[{"targets": ["%s"], "labels": {"instance": "model-host", "host": "model-host", "path": "%s"}}]\n' \
        "$2" "$3" > "/tmp/targets/$1.json"
}
if [ "$CONFIG" = "prometheus.secure.yml" ]; then
    gateway="${SLM_HOSTNAME:-slm.taco.internal}:${GATEWAY_TLS_PORT:-8443}"
    target vllm "$gateway" gateway
    target node "$gateway" direct
    target gpu  "$gateway" direct
else
    target vllm "${MODEL_HOST}:${GATEWAY_PORT:-8080}" gateway
    target node "${MODEL_HOST}:${NODE_EXPORTER_PORT:-9100}" direct
    target gpu  "${MODEL_HOST}:${GPU_EXPORTER_PORT:-9400}" direct
fi

exec /bin/prometheus \
    --config.file="/etc/prometheus/${CONFIG}" \
    --storage.tsdb.path=/prometheus \
    --storage.tsdb.retention.time="${PROMETHEUS_RETENTION:-30d}" \
    --web.enable-lifecycle
