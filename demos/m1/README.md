# Module 1: Hosting a Small Language Model

Runbooks for the three module 1 demonstrations. Everything runs on the GPU host over SSH, with one
check from the desktop.

| Demo | File | Outcome |
| --- | --- | --- |
| 1.1 | [demo-1.1-deploy-and-validate.md](demo-1.1-deploy-and-validate.md) | vLLM serving a quantized candidate behind nginx, validated with the `smoke` tool, deployment record written |
| 1.2 | [demo-1.2-compare-models.md](demo-1.2-compare-models.md) | 22-scenario screening of Qwen, Llama, and Granite with the `screen` tool; comparison table; model selected |
| 1.3 | [demo-1.3-tune-under-load.md](demo-1.3-tune-under-load.md) | Concurrency sweeps with the `load-sweep` tool, memory-pressure and OOM cases, tuned baseline confirmed |

## Components used

| Folder | Used for |
| --- | --- |
| [model-host/](../model-host/README.md) | `vllm` and `gateway` (project `taco-model`), `env.sh`, `serve.sh`, `sample_vram.sh`; one-time GPU host setup |
| [tools/](../tools/README.md) | `smoke`, `screen`, `compare-screening`, `load-sweep`, `compare-load`, `python` one-shots |
| [shared/](../shared/README.md) | screening prompts, grading, policy 2026.09, proposal schema v1, the 22 screening scenarios |
| `results/` | `serving/` (deployment records, serving args, VRAM logs), `screening/`, `sweep/` |

On the GPU host, `source model-host/env.sh` in every shell. It exports `COMPOSE_FILE` so plain
`podman compose ...` targets the model host project, and it defines `tools`, which runs tools-project
commands from any directory (`tools run --rm smoke ...`).

The serving tools accept `--base-url`, `--model`, `--extra-body`, and `--metrics-url`. The base URL
defaults to `INFERENCE_BASE_URL` from `tools/.env` (on the GPU host without that file:
`http://host.containers.internal:8080/v1`, the gateway's published port). With no `--model`, the tools
use the model the server lists.

## What the screening grades

`screen_models.py` (the `screen` tool) scores each scenario on four independent checks and reports the
rate of each:

- **schema_valid**: strict JSON that validates against `proposal.v1.json` (with
  `--structured-output none` this measures the model; with `json_schema` it measures the backend).
- **grounded**: `order_id` matches, `affected_items` are names from the order record, the refund is
  within the order total, and the affected items match the expected set.
- **policy_compliant**: resolution type, refund amount, cited rule ids, and approval flag match the
  policy outcome computed from the inlined facts.
- **category_correct**: complaint category matches.

`passed` requires all four. The weighted score (0.35/0.25/0.30/0.10) is a convenience for sorting,
not a production metric. Tool selection is deliberately absent; module 2 evaluates it with the same
scenarios before the model choice is final.

## Teardown

On the GPU host, see [model-host/README.md](../model-host/README.md#teardown). To re-download weights,
`podman volume rm "$MODEL_CACHE_VOLUME"` after stopping `vllm`.
