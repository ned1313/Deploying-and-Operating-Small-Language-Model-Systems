# Demo 1.2: Compare the three model candidates

Goal: run the same 20 Taco Alley complaints, with order, prior-resolution, and policy facts inlined
in the prompt, against each candidate. Grade schema validity, factual grounding, policy compliance,
latency, token use, and peak VRAM. Pick one model to carry into the tuning demo.

Not graded here: tool selection or tool arguments. That is module 2, using the same scenarios.
This is a screening pass (20 prompts, one run each), not a production benchmark.

Prerequisites: Demo 1.1 state (`gateway` running, tools image built, `source m1/env.sh`).

---

## 1. Look at what the model is being asked to do

```bash
# Twenty scenarios, each with the order record, prior resolutions, and expected outcome
python3 -c '
import json; d=json.load(open("m1/scenarios/screening_scenarios.json"))
for s in d["scenarios"]: print(s["scenario_id"], s["label"].ljust(36), s["expected"]["resolution_type"], s["expected"]["refund_amount"])'

# The policy the model must apply (inlined into the system prompt)
cat m1/scenarios/policy.txt

# The required output schema
python3 -m json.tool m1/scenarios/proposal_schema.json | head -40

# One full prompt as the model sees it
podman compose run --rm tools -c '
import sys; sys.path.insert(0, "tools")
from common import *
s = load_json(DEFAULT_SCENARIOS)["scenarios"][12]           # S13: wrong address, recovered late
print(build_system_prompt(DEFAULT_POLICY.read_text(), load_json(DEFAULT_SCHEMA))[:1200], "...\n")
print(build_user_prompt(s))'
```

Worth pointing out: S04 asks for a refund on an item that is not on the order (grounding trap),
S13 needs R5-then-R3 reasoning, S20 is outside the 14-day window, S02/S03/S11 test the approval
flags. Every fact needed is in the prompt, so a miss is a reasoning or compliance miss.

---

## 2. Screen candidate 1: Qwen (already running from Demo 1.1)

Start the VRAM sampler on the host first, then run the screening container against the gateway
(the `tools` service defaults `BASE_URL` to `http://gateway:8080/v1` and mounts `$RESULTS_DIR`).

```bash
./m1/tools/sample_vram.sh "$RESULTS_DIR/vram-${QWEN_SERVED_NAME}.csv" & SAMPLER=$!

podman compose run --rm tools tools/screen_models.py \
    --model "$QWEN_SERVED_NAME" \
    --extra-body "$QWEN_EXTRA_BODY" \
    --label baseline \
    --vram-log "/app/results/vram-${QWEN_SERVED_NAME}.csv" \
    --include-full-responses

kill $SAMPLER
```

While it runs, narrate the per-scenario line: `schema / grounded / policy / category` flags,
latency, output tokens, and `finish=length` if the model ran out of budget.

Follow-ups worth showing for the first model only:

```bash
# Inspect one failure in detail (replace S13 with whatever failed)
RESULT=$(ls -t "$RESULTS_DIR"/screening-${QWEN_SERVED_NAME}-baseline-*.json | head -1)
python3 -c '
import json,sys; r=json.load(open(sys.argv[1]))
row=[x for x in r["rows"] if x["scenario_id"]=="S13"][0]
print(json.dumps(row["prediction"], indent=2)); print(row["grades"]["issues"])' "$RESULT"

# Same model with server-side guided decoding: schema validity becomes the backend's job.
# Compare grounded/policy rates: constrained decoding fixes shape, not reasoning.
podman compose run --rm tools tools/screen_models.py --model "$QWEN_SERVED_NAME" \
    --extra-body "$QWEN_EXTRA_BODY" --structured-output json_schema --label guided
```

Optional: rerun the baseline with thinking enabled (`--extra-body '{}'`, `--max-tokens 2000`) to show
what reasoning tokens cost in latency for a task that does not need them.

---

## 3. Swap to candidate 2: Llama-3.1-8B-Instruct

One GPU, one model at a time. `serve.sh` recreates the `vllm` service with the new arguments; the
gateway keeps working because the service always gets the same upstream address.

```bash
export HF_TOKEN=hf_...            # gated repo; set in the shell, not in a file
./m1/serve.sh llama
podman compose logs -f vllm       # wait for "Application startup complete"; watch VRAM in nvtop

./m1/tools/sample_vram.sh "$RESULTS_DIR/vram-${LLAMA_SERVED_NAME}.csv" & SAMPLER=$!
podman compose run --rm tools tools/screen_models.py --model "$LLAMA_SERVED_NAME" \
    --extra-body "$LLAMA_EXTRA_BODY" --label baseline \
    --vram-log "/app/results/vram-${LLAMA_SERVED_NAME}.csv" --include-full-responses
kill $SAMPLER
```

---

## 4. Swap to candidate 3: Granite-4.2-8B

```bash
./m1/serve.sh granite
podman compose logs -f vllm

./m1/tools/sample_vram.sh "$RESULTS_DIR/vram-${GRANITE_SERVED_NAME}.csv" & SAMPLER=$!
podman compose run --rm tools tools/screen_models.py --model "$GRANITE_SERVED_NAME" \
    --extra-body "$GRANITE_EXTRA_BODY" --label baseline \
    --vram-log "/app/results/vram-${GRANITE_SERVED_NAME}.csv" --include-full-responses
kill $SAMPLER
```

If a candidate fails to start (unsupported architecture, quantization, or parser name), that is a
Demo 1.1-style compatibility result: record it in the comparison as "not loadable on this
backend/version" rather than scoring it zero on quality.

---

## 5. Compare

```bash
podman compose run --rm tools tools/compare_screening.py --by-scenario 'results/screening-*-baseline-*.json'
```

Columns to discuss:

| column | what it tells you |
| --- | --- |
| `pass` | scenarios where schema, grounding, policy, and category were all correct |
| `schema` / `strict` | did the model emit valid JSON on its own (no guided decoding) |
| `grounded` | order_id, item names, refund within total, correct affected items |
| `policy` | resolution type, amount, rule citations, approval flag |
| `lat p50/p95` | single-request latency through the gateway at concurrency 1 |
| `out tok` / `gen tok/s` | verbosity and raw decode speed at concurrency 1 |
| `peak VRAM` | weights + KV-cache preallocation + runtime overhead at these serving settings |

The per-scenario grid shows *where* each model fails (S=schema, G=grounding, P=policy, C=category),
which matters more than the aggregate at n=20.

---

## 6. Select a model for optimization

Decision rule for the course: highest `pass` rate wins; break ties on `p95` latency, then peak VRAM.
Note the selection is provisional until module 2 confirms tool calling with the same scenarios.

```bash
# Put the chosen candidate back and leave it running for Demo 1.3
./m1/serve.sh qwen        # or llama / granite
podman compose logs -f vllm
```

Deliverables now in `m1/results/`: three `screening-*-baseline-*.json` files, three `vram-*.csv`
logs, and the deployment record for the chosen model.
