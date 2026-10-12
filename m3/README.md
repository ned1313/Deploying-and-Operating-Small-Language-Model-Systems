# Module 3: Orchestrating and Hardening the Agent Workflow

Commands and code for Demos 3.1 and 3.2. The complaint API runs in the same application project as
module 2 (`taco-app`) on the desktop, or on the GPU host for single-machine setups. It consumes the
module 1 inference endpoint and never starts or stops the model server.

| Demo | File | Outcome |
| --- | --- | --- |
| 3.1 | [demo-3.1-orchestrate-and-approve.md](demo-3.1-orchestrate-and-approve.md) | The module 2 loop as a LangGraph workflow in a complaint API: validate, pause for approval, restart, approve, record once |
| 3.2 | [demo-3.2-handle-failures.md](demo-3.2-handle-failures.md) | Clarification, prompt injection, case API and inference failures, malformed output, repeated calls, then the labeled scenarios end to end |

## Layout

```
m3/
  compose.yaml              project taco-app (superset of m2): case-api, complaint-api, seed, cli,
                            failure-tests, e2e-eval, e2e-compare, tests
  .env.example              m2 inference contract + workflow budgets, case API retries, admin token
  secrets/README.md         the same inference_api_key as module 2 (git-ignored, mounted at /run/secrets)
  Containerfile             python:3.12-slim + shared[agent,service,dev] + the module 3 code
  app/
    main.py                 FastAPI complaint API: complaints, cases, reviews, admin, /ui, /graph, /metrics
    service.py              authorization, case records, graph runs, restart resume
    store.py                app.db: cases, proposal versions, review decisions
    security.py             X-Actor-Id -> actor, role, store scope (an unauthenticated identity claim)
    observability.py        request ids, JSON logs, Prometheus metrics, deployment versions (explained in module 4)
    ui.py                   the review page: cases by status, case detail with workflow steps (every value escaped)
  clients/
    taco_cli.py             submit, show, reviews, approve, revise, reject, clarify, retry, graph, reset, case-fault
    api.py                  HTTP helpers shared by the CLI and the harnesses
  eval/
    failure_tests.py        Demo 3.2 harness: expected vs actual per scenario, plus approval checks
    run_e2e_scenarios.py    the 24 labeled scenarios through the API, approved, graded
    compare_e2e.py          m1 screening vs m2 agent vs m3 end to end, per scenario
  tests/                    API, restart, idempotency, harness tests (scripted model, no network)
  results/                  generated output (git-ignored)
```

The workflow itself is in [`../shared/taco_shared/workflow`](../shared/taco_shared/workflow/graph.py)
because modules 4-6 run the same complaint API. It reuses the module 2 model factory, tools, prompts,
proposal call, and validator.

## Identity: two different credentials

- **Application to inference gateway:** the service key in `secrets/inference_api_key`. Every model
  call carries it; module 6 enforces it at NGINX.
- **Person to complaint API:** the `X-Actor-Id` header (or `?actor=` on the review page). The API maps
  it to an actor in `shared/taco_shared/data/actors.json` and derives role and store scope itself. It
  is an identity claim, not authentication, so the API is published on `127.0.0.1` by default
  (see the WSL note under Setup).

| Actor | Role | Stores | May |
| --- | --- | --- | --- |
| `rep-kop-01`, `rep-wg-01`, `rep-multi-01` | service_rep | KOP / WG / all | submit, clarify, retry, view |
| `sup-kop-01`, `sup-wg-01` | reviewer | KOP / WG | approve, revise, reject in their store |
| `sup-area-01` | area_manager | all | approve, revise, reject anywhere |

Nobody may decide on a case they submitted.

## Setup

Requirements: as module 2 (Podman with `podman-compose`; tested with Podman 4.9.3 and podman-compose
1.0.6). The module 1 `vllm` and `gateway` services must be running with the selected model.

```bash
cd m3
cp .env.example .env                   # copy INFERENCE_* values from ../m2/.env
cp ../m2/secrets/inference_api_key secrets/
podman network create taco-telemetry   # once per desktop; module 4 monitoring scrapes the API on it
podman-compose build
podman-compose run --rm seed           # exits 2 if the data came from an older build: add --reset
podman-compose up -d --force-recreate case-api complaint-api
curl -s http://127.0.0.1:8090/healthz
```

`--force-recreate` replaces a `case-api` container started from `m2/` (same project name) with the
module 3 configuration, which enables the admin routes. `seed --reset` rewrites only the synthetic
fixture database.

The complaint API runs up to `WORKFLOW_MAX_CONCURRENT_RUNS` (default 4) workflow runs at once in its
single worker; operations on the same case are serialized, and a request that waits longer than
`WORKFLOW_QUEUE_TIMEOUT_SECONDS` for a slot gets `503` with `Retry-After`. LangSmith tracing is off
unless `TRACING_ENV_FILE` points at a file with `LANGSMITH_*` settings (module 4 uses `../m4/.env`).

### Browser on Windows, Podman in WSL

The ports are published on `127.0.0.1` inside WSL. Windows reaches them only if WSL forwards
localhost, which does not work on every setup (it failed on the test desktop). In that case, set
`PUBLISH_ADDRESS=0.0.0.0` in `.env`, run `podman-compose up -d --force-recreate case-api complaint-api`,
and browse to `http://<WSL address>:8090/ui?actor=sup-kop-01` (`hostname -I` in WSL). With WSL's
default NAT networking that address is reachable only from the Windows host. Do not use `0.0.0.0` on a
host with a LAN-facing interface, because `X-Actor-Id` is not authenticated.

## Services

| Service | Kind | What it does |
| --- | --- | --- |
| `case-api` | long-running | Mock case-management API on `127.0.0.1:8081`; owns `taco-case-data` |
| `complaint-api` | long-running | Complaint API on `127.0.0.1:8090`; owns `taco-app-state` (`checkpoints.db`, `app.db`); reads `taco-app-data`; also on `taco-telemetry` for metrics scraping |
| `seed` | one-shot | Seeds `taco-app-data` |
| `cli` | one-shot | `clients/taco_cli.py` |
| `failure-tests` | one-shot | `eval/failure_tests.py` (uses the admin routes); restores its cases at the end unless `--keep-state` |
| `e2e-eval` | one-shot | `eval/run_e2e_scenarios.py`; restores its cases at the end unless `--keep-state` |
| `e2e-compare` | one-shot | `eval/compare_e2e.py`; `../m1/results` at `/m1-results`, `../m2/results` at `/m2-results` |
| `tests` | one-shot | `pytest` for `shared/tests` and `m3/tests`, with no network |

## Teardown

```bash
podman-compose stop case-api complaint-api      # state is kept in the named volumes
podman volume rm taco-app-state                 # only to forget all cases and checkpoints (destructive)
```

Avoid `podman-compose down` here: the m2 and m3 files share the `taco-app` project name. Stopping this
project never affects the model server on the GPU host.

The two projects also share the `taco-case-data` volume. An action recorded in module 3 makes module 2
see that order as already compensated (R13); `podman-compose run --rm cli reset <case ids>` restores
the fixtures.
