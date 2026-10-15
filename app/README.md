# app

The desktop application: the complaint API (LangGraph workflow, review page, metrics), the mock
case-management API, and the one-shots that act on them. One Podman Compose project, `taco-app`. It
consumes the inference endpoint on the GPU host and never starts or stops the model server.

```
app/
  compose.yaml              project taco-app: case-api, complaint-api (long-running); seed, basic-agent, cli (one-shot)
  .env.example              inference contract, agent and workflow budgets, admin token, ports, LangSmith
  Containerfile             one image (localhost/taco-app) for every service in this project
  complaint_api/
    main.py                 FastAPI complaint API: complaints, cases, reviews, admin, /ui, /graph, /metrics
    service.py              authorization, case records, bounded graph runs, restart resume, trace metadata
    store.py                app.db: cases, proposal versions, review decisions
    security.py             X-Actor-Id -> actor, role, store scope (an unauthenticated identity claim)
    observability.py        request ids, JSON logs, Prometheus metrics, deployment versions, the served-model
                            lookup behind the taco-slm alias, taco_quality_* metrics
    ui.py                   the review page: cases by status, case detail with workflow steps, the quality
                            review queue (/ui/quality); every value escaped
  case_api/main.py          mock case-management API: scoped reads, idempotent approved actions, admin fault
                            injection and fixture reset (admin routes only when TACO_ADMIN_TOKEN is set)
  clients/taco_cli.py       submit, show, reviews, approve, revise, reject, clarify, retry, graph, reset, case-fault
  examples/basic_agent.py   the module 2 teaching script (driver loop in shared/taco_shared/teaching_agent)
  tests/                    API, restart, idempotency, case API, and teaching-script tests
```

The workflow, tools, prompts, validator, and teaching loop are in [`../shared`](../shared/README.md)
because the evaluation tools use them too. Evaluation, load, and report one-shots are in
[`../tools`](../tools/README.md).

## Services by module

| Service | Kind | Introduced | What it does |
| --- | --- | --- | --- |
| `case-api` | long-running | module 2 | Mock case API on `127.0.0.1:8081`; owns `taco-case-data` |
| `seed` | one-shot | module 2 | Seeds `taco-app-data` with customers, orders, and prior resolutions (idempotent; `--reset` is explicit) |
| `basic-agent` | one-shot | module 2 | `examples/basic_agent.py`; database mounted read-only; `--save-transcript` writes to `../results/agent/` |
| `complaint-api` | long-running | module 3 | Complaint API on `127.0.0.1:8090`; owns `taco-app-state` (`checkpoints.db`, `app.db`); reads `taco-app-data`; also on `taco-telemetry` for metrics scraping; from module 5 writes quality samples to `taco-app-quality` |
| `cli` | one-shot | module 3 | `clients/taco_cli.py` |

Runbooks always name the services they start, so module 2 never starts `complaint-api`.

## Identity: two different credentials

- **Application to inference gateway:** the service key in `../secrets/inference_api_key`. Every model
  call carries it; module 6 enforces it at NGINX.
- **Person to complaint API:** the `X-Actor-Id` header (or `?actor=` on the review page). The API maps
  it to an actor in `shared/taco_shared/data/actors.json` and derives role and store scope itself. It
  is an identity claim, not authentication, so the API is published on `127.0.0.1` by default
  (see the WSL note below).

| Actor | Role | Stores | May |
| --- | --- | --- | --- |
| `rep-kop-01`, `rep-wg-01`, `rep-multi-01` | service_rep | KOP / WG / all | submit, clarify, retry, view |
| `sup-kop-01`, `sup-wg-01` | reviewer | KOP / WG | approve, revise, reject in their store |
| `sup-area-01` | area_manager | all | approve, revise, reject anywhere |
| `load-reviewer-01` | reviewer (synthetic) | KOP, WG | used only by the load generator; hidden from the review page |
| `qa-01` | quality_reviewer | all | reads quality samples and records rubric reviews (module 5); cannot submit or approve |

Nobody may decide on a case they submitted.

## Setup

Requirements: Podman with `podman-compose` (tested with Podman 4.9.3 and podman-compose 1.0.6; use the
hyphenated command). No local Python or GPU. The model host's `vllm` and `gateway` must be running
with the selected model.

```bash
cd app
cp .env.example .env            # INFERENCE_BASE_URL, INFERENCE_MODEL, INFERENCE_EXTRA_BODY, TACO_ADMIN_TOKEN
printf 'demo-placeholder-key' > ../secrets/inference_api_key
podman network exists taco-telemetry || podman network create taco-telemetry   # once per desktop
APP_VERSION=$(git rev-parse --short HEAD) podman-compose build
podman-compose run --rm seed    # exits 2 if the data came from an older build: add --reset
podman-compose up -d case-api
podman-compose run --rm basic-agent --check
podman-compose up -d complaint-api                # module 3 onward
curl -s http://127.0.0.1:8090/healthz
```

On Windows PowerShell use `Copy-Item .env.example .env` and
`Set-Content -NoNewline ../secrets/inference_api_key 'demo-placeholder-key'`.

The complaint API runs up to `WORKFLOW_MAX_CONCURRENT_RUNS` (default 4) workflow runs at once in its
single worker; operations on the same case are serialized, and a request that waits longer than
`WORKFLOW_QUEUE_TIMEOUT_SECONDS` for a slot gets `503` with `Retry-After`. LangSmith tracing is off
unless `LANGSMITH_TRACING=true` in `.env` (module 4); recreate `complaint-api` after changing it.

From module 5, `INFERENCE_MODEL=taco-slm` (the stable alias every release is served under; the API reads
the real model name from `/v1/models` and reports it in `/healthz`, `taco_build_info`, and trace
metadata). `QUALITY_SAMPLE_RATE` above 0 writes sampled outcomes to `quality.db` on the
`taco-app-quality` volume; the `quality` tool in `tools/` scores them, and quality reviewers work the
queue at `http://127.0.0.1:8090/ui/quality?actor=qa-01`.

### Browser on Windows, Podman in WSL

The ports are published on `127.0.0.1` inside WSL. Windows reaches them only if WSL forwards
localhost, which does not work on every setup (it failed on the test desktop). In that case, set
`PUBLISH_ADDRESS=0.0.0.0` in `.env`, run `podman-compose up -d --force-recreate case-api complaint-api`,
and browse to `http://<WSL address>:8090/ui?actor=sup-kop-01` (`hostname -I` in WSL). With WSL's
default NAT networking that address is reachable only from the Windows host. Do not use `0.0.0.0` on a
host with a LAN-facing interface, because `X-Actor-Id` is not authenticated.

## Shared state

`taco-case-data` is shared by every module: an action recorded in module 3 (or by an evaluation run
with `--keep-state`) makes the module 2 agent see that order as already compensated (R13).
`podman-compose run --rm cli reset <case ids>` restores the fixtures on both APIs.

## Teardown

```bash
podman-compose stop case-api complaint-api      # state is kept in the named volumes
podman volume rm taco-app-state                 # only to forget all cases and checkpoints (destructive)
podman volume rm taco-app-quality               # only to forget all quality samples, scores, and reviews (destructive)
podman-compose run --rm seed --reset            # only to restore the seeded fixture database (destructive)
```

Avoid `podman-compose down`; never use `down --volumes` for routine shutdown. Stopping this project
never affects the model server on the GPU host or the observability project.
