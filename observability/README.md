# observability

Desktop monitoring: Prometheus and Grafana. One Podman Compose project, `taco-observe`, started in
module 4 and independent of the application: stopping it never stops complaint processing.

```
observability/
  compose.yaml              project taco-observe: prometheus, grafana
  .env.example              MODEL_HOST, exporter and gateway ports, published ports, retention
  prometheus/
    prometheus.yml          jobs complaint-api, vllm, node, gpu, prometheus (stable job/instance labels)
    start.sh                writes the model-host file_sd targets from MODEL_HOST, then starts Prometheus
    rules/taco.yml          recording rules (normalized GPU, latency percentiles) and alerting thresholds
  grafana/
    build_dashboards.py     generates the three dashboards (edit here, then rerun)
    dashboards/             model-serving.json, model-host.json, complaint-workflow.json
    provisioning/           Prometheus data source and dashboard provider
```

Exporters run on the GPU host ([`../model-host`](../model-host/README.md)). Load generation and the
baseline report are one-shots in [`../tools`](../tools/README.md).

## Setup

```bash
cd observability
cp .env.example .env              # MODEL_HOST: the GPU host's LAN address
printf '%s' "$(openssl rand -hex 16)" > ../secrets/grafana_admin_password
podman network exists taco-telemetry || podman network create taco-telemetry
podman-compose up -d prometheus grafana
```

Prometheus is at `http://127.0.0.1:9090` and Grafana at `http://127.0.0.1:3000` (anonymous read-only;
log in as `admin` with the password file to edit). With `PUBLISH_ADDRESS=0.0.0.0` (see the WSL note in
[../app/README.md](../app/README.md#browser-on-windows-podman-in-wsl)), browse to the WSL address instead.

## Services

| Service | Kind | What it does |
| --- | --- | --- |
| `prometheus` | long-running | scrapes `complaint-api` over `taco-telemetry` and the GPU host over the LAN every 5 s; 30 days in `taco-observe-prometheus` |
| `grafana` | long-running | three provisioned dashboards in the "Taco Alley" folder; state in `taco-observe-grafana` |

Prometheus does not expand environment variables in its config, so `start.sh` writes the GPU host
targets (`vllm` through the gateway, `node`, `gpu`) from `MODEL_HOST` when the container starts. The
`vllm` job scrapes the gateway without authentication in modules 4-5; module 6 moves it to an
authenticated route and keeps `job="vllm"`, `instance="model-host"` so dashboards keep working.

## Teardown

```bash
podman-compose stop prometheus grafana          # monitoring history stays in the volumes
```

Never use `podman-compose down --volumes` for routine shutdown: it deletes the monitoring history.
