# Module 2: Building an SLM Agent with Tools

Runbook for Demo 2.1. The application project (`taco-app`) runs on the desktop, or on the GPU host for
single-machine setups. It consumes the inference endpoint from module 1 and never starts or stops the
model server.

| Demo | File | Outcome |
| --- | --- | --- |
| 2.1 | [demo-2.1-investigation-loop.md](demo-2.1-investigation-loop.md) | Plain chat call, then a LangChain `create_agent` agent with typed tools, a structured proposal, validation, and an agent evaluation that confirms the model selection for tool calling |

## Components used

| Folder | Used for |
| --- | --- |
| [app/](../app/README.md) | `case-api`, `seed`, and the `basic-agent` teaching script ([examples/basic_agent.py](../app/examples/basic_agent.py)); setup |
| [tools/](../tools/README.md) | `agent-eval` and `agent-compare` |
| [shared/](../shared/README.md) | the teaching loop (`taco_shared/teaching_agent/`), model factory, tools, prompts, validator, database access, dataset |
| [model-host/](../model-host/README.md) | the running `vllm` and `gateway` from module 1 |
| `results/` | `agent/` (evaluation results and saved transcripts); reads `screening/` for the comparison |

Requirements on the desktop: Podman with `podman-compose` (tested with Podman 4.9.3 and podman-compose
1.0.6; use the hyphenated command). No local Python or GPU. Follow the setup in
[app/README.md](../app/README.md#setup) up to `basic-agent --check` (module 2 does not start
`complaint-api`), and copy `tools/.env.example` to `tools/.env` with the same inference values.
