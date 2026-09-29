# Module 1: Hosting a Small Language Model

Commands and helper scripts for the three module 1 demonstrations. Everything runs on the GPU host
over SSH; the model server, gateway, and evaluation tools are services in one Podman Compose
project ([compose.yaml](compose.yaml)).

| Demo | File | Outcome |
| --- | --- | --- |
| 1.1 | [demo-1.1-deploy-and-validate.md](demo-1.1-deploy-and-validate.md) | vLLM serving a quantized candidate behind nginx, validated with `smoke_test.py`, deployment record written |
| 1.2 | [demo-1.2-compare-models.md](demo-1.2-compare-models.md) | 20-scenario screening of Qwen, Llama, and Granite with `screen_models.py`; comparison table; model selected |
| 1.3 | [demo-1.3-tune-under-load.md](demo-1.3-tune-under-load.md) | Concurrency sweeps with `load_sweep.py`, memory-pressure and OOM cases, tuned baseline confirmed |

## Layout

```
m1/
  compose.yaml              Podman Compose project: vllm (GPU via CDI), gateway (nginx), tools (disposable client)
  env.sh                    host settings, image tags, COMPOSE_FILE, and the three candidate definitions (edit before recording)
  serve.sh                  build VLLM_ARGS for a candidate and `podman compose up -d --force-recreate vllm`
  gateway/nginx.conf        unauthenticated reverse proxy on :8080 -> fixed vllm upstream address (module 6 hardens it)
  scenarios/
    screening_scenarios.json  20 synthetic complaints with order, prior-resolution facts, and expected outcomes
    policy.txt                resolution policy inlined into the system prompt (rules R1-R12)
    proposal_schema.json      required JSON output schema
  tools/
    Containerfile, requirements.txt   tools image (python:3.12-slim + openai + jsonschema), built by `podman compose build tools`
    common.py                 prompt construction, JSON parsing, /metrics scraping, shared CLI flags
    smoke_test.py             Demo 1.1 endpoint validation + deployment record
    screen_models.py          Demo 1.2 screening with schema/grounding/policy grading
    compare_screening.py      Demo 1.2 side-by-side table and per-scenario pass/fail grid
    load_sweep.py             Demo 1.3 concurrency sweep with TTFT, throughput, queue and KV-cache metrics
    sample_vram.sh            host-side nvidia-smi sampler; feeds --vram-log for peak VRAM
  results/                  generated JSON/CSV output (git-ignored)
```

## One-time setup on the GPU host

Requirements: Podman 4.x+ with `podman compose` (the `podman-compose` package or the docker-compose
provider), the NVIDIA driver, and the NVIDIA Container Toolkit for CDI device injection.

```bash
git clone https://github.com/ned1313/Deploying-and-Operating-Small-Language-Model-Systems && cd Deploying-and-Operating-Small-Language-Model-Systems
chmod +x m1/serve.sh m1/tools/sample_vram.sh
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml     # expose the GPU to podman as nvidia.com/gpu=all
source m1/env.sh                                                # exports COMPOSE_FILE so `podman compose` works anywhere
podman compose build tools
```

Before recording, open [env.sh](env.sh) and:

1. Replace the placeholder Hugging Face repository ids with verified vLLM-loadable quantized
   artifacts, and pin `*_REVISION` to a commit sha.
2. Pin `VLLM_IMAGE` to a specific tag; confirm the `--reasoning-parser` / `--tool-call-parser`
   names exist in that tag (`podman run --rm "$VLLM_IMAGE" --help | grep -A5 parser`).
3. Export `HF_TOKEN` in the shell for gated repositories. Compose passes it to the container by
   name; nothing in this folder writes it to disk.
4. If `10.89.7.0/24` overlaps your LAN, change `INFERENCE_SUBNET` / `VLLM_UPSTREAM_IP` and the
   `server` line in [gateway/nginx.conf](gateway/nginx.conf).

## Running the tools

All tools accept `--base-url`, `--model`, `--extra-body`, and `--metrics-url`, or the equivalent
`BASE_URL`, `MODEL`, `EXTRA_BODY`, `METRICS_URL` environment variables. The `tools` service defaults
`BASE_URL` to `http://gateway:8080/v1` and mounts `$RESULTS_DIR` at `/app/results`.

```bash
# On the GPU host, over the compose network
podman compose run --rm tools tools/<script>.py ...

# From the desktop, through the LAN (same compose file; only the tools service is used)
BASE_URL=http://<MODEL_HOST>:8080/v1 podman compose run --rm tools tools/<script>.py ...

# Without containers (any machine with Python 3.10+)
pip install -r m1/tools/requirements.txt
python m1/tools/<script>.py --base-url http://<MODEL_HOST>:8080/v1 ...
```

## What the screening grades

`screen_models.py` scores each scenario on four independent checks and reports the rate of each:

- **schema_valid**: strict JSON that validates against `proposal_schema.json` (with
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

```bash
podman compose down                        # stops vllm and gateway, removes the compose network
podman volume rm "$MODEL_CACHE_VOLUME"     # only if you want to re-download weights
```
