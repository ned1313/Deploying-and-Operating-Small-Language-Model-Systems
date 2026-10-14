# tools

Disposable evaluation, load, and report commands, plus the test suites. One Podman Compose project,
`taco-tools`, with one-shot services only, built from one image (`localhost/taco-tools`). Runs on
either host against a configured URL; nothing here is long-running.

```
tools/
  compose.yaml              project taco-tools: one service per command (below)
  .env.example              target URL, model settings for agent-eval and direct load, admin token, execution host
  Containerfile             python:3.12-slim + shared[agent,service,dev,load] + taco_tools
  taco_tools/
    serving/                module 1: smoke_test, screen_models, compare_screening, load_sweep, compare_load
    agent_eval/             module 2: run_agent_scenarios, compare_agent_vs_screening
    e2e/                    module 3: failure_tests, run_e2e_scenarios, compare_e2e
    load/                   module 4: Locust workloads (locustfile) and the headless runner (run)
    reports/                module 4: baseline_report
  tests/                    harness and agent-eval tests (reuse the app test fixtures)
```

## Setup

```bash
cd tools
cp .env.example .env              # INFERENCE_BASE_URL and, on the desktop, the values shared with app/.env
podman-compose build
```

On the GPU host, use the `tools` function from `model-host/env.sh` instead of `cd tools`
(`tools build`, `tools run --rm smoke ...`); without a `tools/.env` there, `INFERENCE_BASE_URL`
defaults to `http://host.containers.internal:8080/v1`.

## Services

All results go to `../results/<kind>/` on the machine that runs the command. Compare commands take
paths relative to the container's working directory, for example `'results/screening/screening-*.json'`
(quoted globs are expanded by the script).

| Service | Module | Needs | What it does |
| --- | --- | --- | --- |
| `smoke` | 1 | gateway URL | endpoint validation and deployment record -> `results/serving/` |
| `screen` | 1 | gateway URL | 22-scenario screening with facts inlined -> `results/screening/` |
| `compare-screening` | 1 | results | side-by-side screening table and per-scenario grid |
| `load-sweep` | 1 | gateway URL | concurrency sweep with TTFT, queue, and KV-cache metrics -> `results/sweep/` |
| `compare-load` | 1 | results | side-by-side sweep table and CSV -> `results/sweep/` |
| `python` | any | - | `python` in the tools image, for one-off snippets (`-c '...'`) |
| `agent-eval` | 2 | app (`case-api`, seeded data) | the 24 curated scenarios in agent form -> `results/agent/` |
| `agent-compare` | 2 | results | per-scenario grid: screening vs agent |
| `failure-tests` | 3 | app (`complaint-api`) | expected vs actual for the workflow scenarios plus approval checks -> `results/failure/` |
| `e2e-eval` | 3 | app (`complaint-api`) | the 24 labeled scenarios through the API, approved, graded -> `results/e2e/` |
| `e2e-compare` | 3 | results | screening vs agent vs end to end, per scenario |
| `load` | 4 | app, gateway URL | Locust `--mode direct|app`, `--shape steady|burst` -> `results/load/` |
| `baseline` | 4 | observability (`prometheus`) | Prometheus over a load run's window + Locust numbers -> `results/baseline/` |
| `tests` | any | - | `shared/tests`, `app/tests`, `tools/tests` against the mounted source, no network |

Services that need the app join the external `taco-app-private` network, `baseline` joins
`taco-telemetry`, and `agent-eval` mounts the app's `taco-app-data` volume read-only, so those run on
the desktop after `app/` (and `observability/`) are up. The serving services only need the gateway URL.

`failure-tests` and `e2e-eval` reset the cases they use through the admin routes (`TACO_ADMIN_TOKEN`
must match `app/.env`) and restore them when they finish unless `--keep-state`. App-mode `load` resets
its 150 `load-v1` cases before each run (`--no-reset` to skip), which never touches the curated cases.
