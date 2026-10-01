# Module 2: Building an SLM Agent with Tools

Commands and code for Demo 2.1. The application project (`taco-app`) runs on the desktop, or on the
GPU host for single-machine setups. It consumes the module 1 inference endpoint and never starts or
stops the model server.

| Demo | File | Outcome |
| --- | --- | --- |
| 2.1 | [demo-2.1-investigation-loop.md](demo-2.1-investigation-loop.md) | Plain chat call, then a LangChain `create_agent` agent with typed tools, a structured proposal, validation, and an agent evaluation that confirms the model selection for tool calling |

## Layout

```
m2/
  compose.yaml              project taco-app: case-api (long-running) + one-shot seed, basic-agent, agent-eval, agent-compare, tests
  .env.example              inference contract and agent limits (copy to .env, git-ignored)
  secrets/README.md         how to create secrets/inference_api_key (git-ignored, mounted at /run/secrets)
  Containerfile             python:3.12-slim + shared[agent,service,dev] + the module 2 code
  agent/
    loop.py                 driver loop around create_agent: stream steps, finalize with json_schema, validate
    render.py               step-by-step console output
    scenarios.py            scenario lookup and wiring of tools to the database and case API
  examples/basic_agent.py   the teaching script (clips 2-4)
  eval/
    run_agent_scenarios.py  the 24 curated scenarios in agent form, tool-calling and proposal grading
    compare_agent_vs_screening.py   per-scenario grid: module 1 screening vs module 2 agent
  tests/                    loop, script, and eval tests with a scripted chat model (no GPU, no network)
  results/                  generated output (git-ignored)
```

Shared pieces (model factory, tools, prompts, validator, database access, dataset, mock case API) live
in [`../shared`](../shared/README.md) so module 3 can reuse them in the complaint API.

## Setup

Requirements: Podman with `podman-compose` (tested with Podman 4.9.3 and podman-compose 1.0.6; use
the hyphenated command). No local Python or GPU. The module 1
`vllm` and `gateway` services must be running on the GPU host with the selected model.

```bash
cd m2
cp .env.example .env            # set INFERENCE_BASE_URL, INFERENCE_MODEL, INFERENCE_EXTRA_BODY
printf 'demo-placeholder-key' > secrets/inference_api_key
podman-compose build
podman-compose run --rm seed     # idempotent; `--reset` restores the fixtures (destructive)
podman-compose up -d case-api
podman-compose run --rm basic-agent --check
```

On Windows PowerShell use `Copy-Item .env.example .env` and
`Set-Content -NoNewline secrets/inference_api_key 'demo-placeholder-key'`.

## Services

| Service | Kind | What it does |
| --- | --- | --- |
| `case-api` | long-running | Mock case-management API on `127.0.0.1:8081`, own SQLite volume `taco-case-data` |
| `seed` | one-shot (`setup`) | Seeds `taco-app-data` with customers, orders, and prior resolutions |
| `basic-agent` | one-shot (`demo`) | `examples/basic_agent.py`; database mounted read-only |
| `agent-eval` | one-shot (`demo`) | `eval/run_agent_scenarios.py` |
| `agent-compare` | one-shot (`demo`) | `eval/compare_agent_vs_screening.py`; `../m1/results` mounted at `/m1-results` |
| `tests` | one-shot (`test`) | `pytest` for `shared/tests` and `m2/tests`, with no network |

## Teardown

```bash
podman-compose down                   # stops case-api; volumes and seeded data are kept
podman volume rm taco-app-data taco-case-data   # only to start over (destructive)
```

Stopping this project never affects the model server on the GPU host.
