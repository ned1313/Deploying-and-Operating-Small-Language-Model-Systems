# Demo 1.1: Deploy and validate the serving endpoint

Goal: start a quantized candidate on the RTX 3090 with vLLM, expose it through an nginx gateway,
call the REST API, verify request/response formatting and structured-output support, and record
the deployment configuration. Show one compatibility failure and label it as such.

Run everything on the GPU host over SSH. Keep `nvtop` open in a second terminal.

---

## 0. Prerequisites check (first time only)

```bash
cd ~/Deploying-and-Operating-Small-Language-Model-Systems   # adjust to your clone path
source m1/env.sh
chmod a+x m1/tools/*.sh

# Podman and the compose provider
podman version --format 'client={{.Client.Version}} server={{.Server.Version}}'
podman compose version

# GPU visible to the host, and exposed to containers through CDI
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml     # once per driver update
nvidia-ctk cdi list                                             # expect nvidia.com/gpu=all
podman run --rm --device nvidia.com/gpu=all docker.io/library/ubuntu nvidia-smi --query-gpu=name,memory.used --format=csv

# Pull/build images ahead of time so the recording does not wait on downloads
podman pull "$VLLM_IMAGE"
podman pull "$NGINX_IMAGE"
podman compose build tools
```

> Gated repositories (Llama) need `export HF_TOKEN=hf_...` in this shell before `serve.sh`.
> Compose reads it from the shell (`HF_TOKEN: ${HF_TOKEN:-}`); it is never written to a file in this repo.

---

## 1. Start the first candidate

Everything in module 1 is one Compose project, [compose.yaml](compose.yaml): a `vllm` service whose
arguments come from `VLLM_ARGS`, an nginx `gateway`, and a disposable `tools` client. `serve.sh`
builds `VLLM_ARGS` for a candidate, saves it to `results/last-serving-args.txt`, and runs
`podman compose up -d --force-recreate vllm`.

```bash
./m1/serve.sh qwen --dry-run          # talk through the arguments before running
./m1/serve.sh qwen
podman compose logs -f vllm            # Ctrl-C once you see "Application startup complete"
```

Things to call out while it loads (watch nvtop):

- weights download to the `hf-cache` volume on first run only
- VRAM jumps to the quantized weights, then the KV-cache preallocation fills up to
  `--gpu-memory-utilization` of the card; the "GPU KV cache size" / "Maximum concurrency" log lines
  show how much room is left for context at `--max-model-len`
- the CUDA graph capture step is one-time startup cost, not per-request cost

```bash
# Only port 8000 on loopback is published; nothing on the LAN can reach vLLM directly.
podman port vllm
podman compose ps
curl -s -o /dev/null -w 'health=%{http_code}\n' "http://127.0.0.1:${VLLM_PORT}/health"
```

---

## 2. Call the API directly with curl

```bash
# Which model is served, and what context length did the server settle on?
curl -s "${BASE_URL_DIRECT}/models" | jq

# A minimal OpenAI-style chat completion. Note the request shape: model, messages[], sampling knobs.
curl -s "${BASE_URL_DIRECT}/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "'"$QWEN_SERVED_NAME"'",
    "messages": [
      {"role": "system", "content": "You are a concise assistant for Taco Alley restaurant staff."},
      {"role": "user", "content": "A customer says their guacamole was missing. What is the first thing to check?"}
    ],
    "temperature": 0,
    "max_tokens": 120,
    "chat_template_kwargs": {"enable_thinking": false}
  }' | jq
```

Point out in the response: `choices[0].message.content`, `finish_reason` (`stop` vs `length`),
and `usage.prompt_tokens` / `usage.completion_tokens`. Those usage numbers drive the cost and
KV-cache math later in the module.

```bash
# Same request, streamed. Time-to-first-token is the prefill cost; the rest is decode.
curl -sN "${BASE_URL_DIRECT}/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"'"$QWEN_SERVED_NAME"'","stream":true,"max_tokens":60,
       "messages":[{"role":"user","content":"List three taco fillings."}],
       "chat_template_kwargs":{"enable_thinking":false}}' | head -n 12
```

---

## 3. Put the nginx gateway in front of vLLM

The gateway proxies to the fixed address compose assigns the `vllm` service (`VLLM_UPSTREAM_IP`),
so swapping models later never requires touching nginx.

```bash
podman compose up -d gateway

podman compose logs gateway --tail 5
curl -s "${BASE_URL_LOCAL}/models" | python3 -c 'import json,sys; print([m["id"] for m in json.load(sys.stdin)["data"]])'

# The gateway logs request time and upstream time separately; useful in module 4.
podman compose logs gateway --tail 2
```

The application in later modules only ever knows `http://${MODEL_HOST}:${GATEWAY_PORT}/v1`.
Module 6 adds TLS and service keys to this same nginx config; vLLM itself never changes.

---

## 4. Validate with the smoke-test container

The `tools` service has no GPU dependency and no local Python requirement. It talks to the gateway
over the compose network (`BASE_URL` defaults to `http://gateway:8080/v1`), exactly the way a
desktop container will.

```bash
podman compose run --rm tools tools/smoke_test.py \
    --extra-body "$QWEN_EXTRA_BODY" \
    --hf-repo "$QWEN_REPO" --revision "$QWEN_REVISION" \
    --quantization "$QWEN_QUANT_LABEL" \
    --chat-template "tokenizer default" \
    --reasoning-parser "$QWEN_REASONING_PARSER" \
    --tool-call-parser "$QWEN_TOOL_PARSER" \
    --serving-image "$VLLM_IMAGE" \
    --serving-args "$(cat "$RESULTS_DIR/last-serving-args.txt")"
```

What the script does, in order:

1. `GET /v1/models`: confirms the served name and `max_model_len` the server actually applied.
2. Plain chat completion: prints the request body and the trimmed response.
3. `response_format: json_schema` with the resolution-proposal schema against scenario S01;
   validates the reply with `jsonschema`. Guided decoding is a *backend* capability, so a 400 here
   is a compatibility issue, not a model-quality signal.
4. Writes `results/deployment-record-<model>.json` with server facts plus your pins. The
   tool-call parser is recorded as a prerequisite for module 2 and is not exercised here.

```bash
cat "$RESULTS_DIR/deployment-record-${QWEN_SERVED_NAME}.json"
```

### Verify from the desktop (second machine)

```bash
# On the desktop, after cloning the repo. The same compose file works; only the tools service is used.
source m1/env.sh
podman compose build tools
BASE_URL="http://<MODEL_HOST>:8080/v1" podman compose run --rm tools tools/smoke_test.py --skip-structured \
  --extra-body '{"chat_template_kwargs": {"enable_thinking": false}}'
```

If this fails while step 4 passed, the problem is network or firewall on the GPU host
(`sudo ufw status`), not the model server.

---

## 5. Show a compatibility failure (not a model-quality failure)

### 5a. Wrong quantization flag at startup

vLLM auto-detects the quantization format from the checkpoint config. Forcing a different one
fails fast, before any weights load:

```bash
./m1/serve.sh qwen --quantization gptq
podman compose logs -f vllm    # ends with a ValueError about quantization method mismatch
```

Read the error out loud: it names the checkpoint's format and the flag you passed. Nothing about
the model's abilities was tested. Restore the working deployment:

```bash
./m1/serve.sh qwen
podman compose logs -f vllm    # wait for "Application startup complete"
```

### 5b. Request that violates the serving configuration at runtime

```bash
# Send a prompt roughly twice --max-model-len. The server rejects it before inference.
python3 -c '
import json, sys
filler = "The customer reports that the guacamole was missing from the bag. " * (int(sys.argv[2]) // 6)
print(json.dumps({"model": sys.argv[1], "max_tokens": 50,
                  "messages": [{"role": "user", "content": filler + "Summarize the complaint."}],
                  "chat_template_kwargs": {"enable_thinking": False}}))' "$QWEN_SERVED_NAME" "$MAX_MODEL_LEN" \
| curl -s -w '\nHTTP %{http_code}\n' "${BASE_URL_LOCAL}/chat/completions" -H 'Content-Type: application/json' -d @-
```

The 400 message states the configured context length and how many input tokens were sent. The
client and server disagree about limits; the model never ran, and nvtop shows no GPU spike.
`smoke_test.py` labels 4xx responses `COMPATIBILITY ISSUE` for the same reason.

Contrast worth mentioning: an oversized `max_tokens` (e.g. 20000 with a short prompt) is *not*
rejected by current vLLM; it is capped to the remaining context. Limits the server silently adjusts
are easy to miss, which is why the deployment record captures `max_model_len`. Demo 1.3 revisits
`--max-model-len` deliberately.

---

## 6. Wrap-up state for the next demo

Leave `vllm` (Qwen candidate) and `gateway` running. Demo 1.2 swaps candidates with `serve.sh`;
the gateway never needs to restart because the `vllm` service always gets the same address.

```bash
podman compose ps
```
