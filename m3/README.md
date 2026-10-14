# Module 3: Orchestrating and Hardening the Agent Workflow

Runbooks for Demos 3.1 and 3.2. The complaint API runs in the application project (`taco-app`) on
the desktop, or on the GPU host for single-machine setups. It consumes the inference endpoint from
module 1 and never starts or stops the model server.

| Demo | File | Outcome |
| --- | --- | --- |
| 3.1 | [demo-3.1-orchestrate-and-approve.md](demo-3.1-orchestrate-and-approve.md) | The module 2 loop as a LangGraph workflow in a complaint API: validate, pause for approval, restart, approve, record once |
| 3.2 | [demo-3.2-handle-failures.md](demo-3.2-handle-failures.md) | Clarification, prompt injection, case API and inference failures, malformed output, repeated calls, then the labeled scenarios end to end |

## Components used

| Folder | Used for |
| --- | --- |
| [app/](../app/README.md) | `complaint-api` ([complaint_api/](../app/complaint_api/)), `case-api`, the `cli`, and the review page; setup, identities, and the WSL browser note |
| [tools/](../tools/README.md) | `failure-tests`, `e2e-eval`, `e2e-compare` |
| [shared/](../shared/README.md) | the LangGraph workflow ([workflow/graph.py](../shared/taco_shared/workflow/graph.py)), reusing the module 2 model factory, tools, prompts, proposal call, and validator |
| [model-host/](../model-host/README.md) | the running `vllm` and `gateway`; stopped once in Demo 3.2 |
| `results/` | `failure/`, `e2e/`; reads `screening/` and `agent/` for the comparison |

Setup: [app/README.md](../app/README.md#setup) through `up -d complaint-api`, and `tools/.env` with the
same `TACO_ADMIN_TOKEN` as `app/.env` (the harnesses use the admin routes for fault injection and
resets).

## Teardown

```bash
cd app && podman-compose stop case-api complaint-api      # state is kept in the named volumes
```

The case API volume is shared with module 2. An action recorded here makes module 2 see that order as
already compensated (R13); `podman-compose run --rm cli reset <case ids>` (from `app/`) restores the
fixtures.
