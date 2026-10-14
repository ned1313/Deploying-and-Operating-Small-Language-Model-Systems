#!/bin/sh
# Writes the model-host scrape targets from the environment, then starts Prometheus.
set -eu
: "${MODEL_HOST:?set MODEL_HOST in m4/.env}"
mkdir -p /tmp/targets

target() {  # file, port, path label
    printf '[{"targets": ["%s:%s"], "labels": {"instance": "model-host", "host": "model-host", "path": "%s"}}]\n' \
        "$MODEL_HOST" "$2" "$3" > "/tmp/targets/$1.json"
}
target vllm "${GATEWAY_PORT:-8080}" gateway
target node "${NODE_EXPORTER_PORT:-9100}" direct
target gpu  "${GPU_EXPORTER_PORT:-9400}" direct

exec /bin/prometheus \
    --config.file=/etc/prometheus/prometheus.yml \
    --storage.tsdb.path=/prometheus \
    --storage.tsdb.retention.time="${PROMETHEUS_RETENTION:-30d}" \
    --web.enable-lifecycle
