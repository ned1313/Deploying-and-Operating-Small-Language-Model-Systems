# Demo 2.1: Build the investigation loop

Goal: connect a LangChain agent to the module 1 endpoint, first with a plain chat call and then with
typed tools that read an order and prior resolutions from a database, retrieve policy, and query a
mock case-management API. Follow a missing-items complaint to a structured resolution proposal,
validate it, and confirm tool-calling reliability for the model chosen in module 1.

Prerequisites: module 1 final state (`vllm` + `gateway` running with the selected model, screening
rerun on `screening-v2`), and the setup in [README.md](README.md) (`.env`, secret, `seed`, `case-api`).
Commands run from `m2/`.

Expected-output excerpts below were captured against `llama-3.1-8b`; numbers and wording will differ
with another model.

---

## Clip 2: a basic agent with an SLM

### 1. Configuration shared by the script and (in module 3) the complaint API

```bash
cat .env.example          # inference contract: base URL, model id, per-model extras, limits
cat secrets/README.md     # the credential is a mounted file, never an env var or a command-line flag
sed -n '/x-inference-env/,/RESULTS_DIR/p' compose.yaml
```

Talking points: the application only needs `INFERENCE_BASE_URL` and `INFERENCE_MODEL`; moving the
model to another host is a configuration change. The key file is a placeholder until module 6.

### 2. Validate connectivity from a container

```bash
podman-compose run --rm basic-agent --check
```

```
Inference endpoint: http://192.168.1.251:8080/v1   model: llama-3.1-8b   key: secret file /run/secrets/inference_api_key
  served: llama-3.1-8b  max_model_len=4096  root=RedHatAI/Meta-Llama-3.1-8B-Instruct-quantized.w8a8
  chat ok in 0.09s: 'Ready'
Application database: /data/app/taco_app.db (policy 2026.09, dataset agent-v1)
Case API: http://case-api:8081 healthy
```

If the first lines fail, the output says whether it is a network/gateway problem or a configuration
mismatch (wrong model id); neither is a model-quality signal.

### 3. Walk through the code

- `shared/taco_shared/agent/model.py`: `build_chat_model()` is `ChatOpenAI` pointed at vLLM:
  `base_url`, the mounted key, `temperature=0`, `seed=42`, `extra_body` for per-model switches, and
  `use_responses_api=False` because vLLM is used through Chat Completions.
- `m2/agent/loop.py`, `run_chat()`: one system prompt, one complaint, one call. No tools, no history.

### 4. Submit a complaint with no tools

```bash
podman-compose run --rm basic-agent --mode chat --scenario A01
```

```
Complaint: Two of the three carnitas tacos I ordered were missing from my delivery bag. Everything else was there. Order TA-10431.

- A complimentary replacement of the missing carnitas tacos (2 tacos) for Order TA-10431.
- A $5 credit to your account for the inconvenience caused.
...
No order, history, or policy was looked up: every fact above is the model's guess.
```

Discuss: the answer sounds reasonable but nothing in it was checked. The credit is invented, the
price is unknown, and the model cannot know the customer's history or the current policy.

---

## Clip 3: tool calling and orchestration (printouts for the slides)

```bash
podman-compose run --rm basic-agent --show-tools
```

Shows the four tools exactly as sent to the model (`convert_to_openai_tool`) and the v2 proposal
schema used for the finalize call. Points to make:

- Tool arguments are only what the model may choose: `order_id`, `lookback_days`,
  `complaint_category`. The customer, store scope, and case come from the application and never
  appear in the schema.
- Tool calling needs server support: module 1 started vLLM with `--enable-auto-tool-choice
  --tool-call-parser <parser>` (`m1/serve.sh`), recorded in the module 1 deployment record.
- `create_agent` runs the model/tool cycle; the script still owns the loop around it (streaming each
  step, limits, the finalize call, validation). Module 3 adds durable state, routing, and approval.
- Structured output is a separate, tool-free call with `response_format: json_schema`. Sending the
  schema together with tools would make guided decoding constrain every turn and block tool calls.

---

## Clip 4: calling tools with the agent

### 1. Walk through the code

- `shared/taco_shared/agent/tools.py`: Pydantic argument models, trusted context bound in the
  closure, typed `not_found` / `invalid_arguments` / `unavailable` results instead of exceptions.
- `shared/taco_shared/db.py`: narrow, read-only, scoped queries (the order must belong to the
  customer and to one of the actor's stores). No arbitrary SQL.
- `shared/taco_shared/case_api_client.py` and the mock API: the store scope travels as a header.
- `m2/agent/loop.py`, `run_investigation()`: `create_agent(...)` with `ToolCallLimitMiddleware`, the
  `stream_mode="updates"` loop, then `finalize()` with `with_structured_output(..., method="json_schema")`.
- `shared/taco_shared/proposal.py`, `validate_proposal()`: schema plus business rules checked only
  against evidence the tools returned.

### 2. The mock case-management API

```bash
curl -s -H 'X-Store-Scope: KOP' http://127.0.0.1:8081/cases/CASE-10431 | python3 -m json.tool
curl -s -o /dev/null -w '%{http_code}\n' -H 'X-Store-Scope: WG' http://127.0.0.1:8081/cases/CASE-10431   # 404
```

### 3. Follow the missing-items complaint

```bash
podman-compose run --rm basic-agent --mode tools --scenario A01
```

```
Trusted context: actor=rep-kop-01 stores=KOP customer=CUST-14159 case=CASE-10431  (never sent as tool arguments)

[1] model -> tool_call get_order {"order_id": "TA-10431"}                  0.4s 21 tok
    tool   <- get_order: ok order:TA-10431  3 items, total 27.84, store KOP
[2] model -> tool_call get_prior_resolutions {"lookback_days": "90"}       0.3s 21 tok
    tool   <- get_prior_resolutions: ok 1 resolution(s) in the last 90 days
[3] model -> tool_call get_resolution_policy {"complaint_category": "missing_items"}   0.3s 21 tok
    tool   <- get_resolution_policy: ok policy 2026.09: R1, R4, R7, R9, R10, R11, R12, R13, R14
[4] model -> tool_call get_case_status {}                                  0.3s 14 tok
    tool   <- get_case_status: ok case:CASE-10431  status open, 0 recorded action(s)
[5] model -> answer                                                        2.8s 232 tok
[6] finalize (json_schema) -> ProposalV2                                   3.1s 239 tok
{ "order_id": "TA-10431", "resolution_type": "partial_refund", "refund_amount": 8.5, ... }
    schema: valid   business rules: 0 violation(s)   ACCEPTED (pending human approval in module 3)
```

The correct outcome is a partial refund of 8.50 (2 x 4.25, R1). Narrate the model choosing each tool,
the order id it extracted from the complaint text, and the evidence references in the proposal.

When the model gets it wrong, the validator shows why structured output alone is not enough. A run
captured with Llama 3.1 8B proposed a full refund of 27.84 under R4 without the approval flag:

```
    schema: valid   business rules: 1 violation(s)   REJECTED
    rule violation: monetary resolution over 25.00 requires human approval (R9)
```

Valid JSON, wrong decision. Keep a saved transcript of both outcomes as recording fallbacks
(`--save-transcript` writes to `results/`).

### 4. Scope: the same complaint from a representative at another store

```bash
podman-compose run --rm basic-agent --mode tools --scenario A01 --actor rep-wg-01
```

`get_order` and `get_case_status` return `not_found`; the validator rejects any proposal that names
an order the tools did not return (`order_id was not returned by get_order`). The scope check is in
the tool and the database query, not in the prompt.

### 5. Already compensated (R13)

```bash
podman-compose run --rm basic-agent --mode tools --scenario A02
```

The case API shows a refund already recorded for the order. A correct proposal is `no_action` citing
R13; a monetary proposal is rejected with `this order was already compensated (R13)`.

### 6. Optional: prompt-only JSON

```bash
podman-compose run --rm basic-agent --mode tools --scenario A01 --no-structured-output
```

The finalize call runs without `response_format`. Failures to parse or schema errors are not
guaranteed on every run; use a saved transcript if the live run happens to be clean.

### 7. What this loop lacks

Close on the comments in `m2/agent/loop.py`: no checkpointer (a restart loses the run), no custom
routing (clarification, escalation), only a tool-call cap rather than retry budgets, and no approval
pause before anything affects the customer. Module 3 adds these with LangGraph.

---

## Demo 2.1: confirm the model selection for tool calling

```bash
podman-compose run --rm agent-eval --label agent-baseline
```

Per scenario: `parsed` (backend parsed every tool call), `args` (get_order called with the quoted
order id), `required` (order, history, and policy looked up), `budget`, `valid` (validator accepted),
then the same grounding and policy grades as module 1.

```
Agent evaluation summary
  tool_calls_parsed:      1.00
  tool_args_valid:        1.00
  required_tools_called:  1.00
  args_correct:           1.00
  within_budget:          1.00
  proposal_accepted:      0.79
  ...
  passed:                 0.21
```

Compare with the module 1 screening for the same model (same dataset build):

```bash
podman-compose run --rm agent-compare \
    '/m1-results/screening-<model>-baseline-*.json' 'results/agent-<model>-agent-baseline-*.json'
```

The grid shows each scenario's module 1 result next to the agent result and its tool checks.
Discussion: tool calling can be perfectly reliable while decisions get worse, because the agent must
now find and combine facts that module 1 handed it. Selection gate (planning section 6.7): tool calls
parsed 100%, required tools >= 90%, arguments correct >= 95%, schema valid 100%, and an agent pass rate
no more than 10 points below the screening pass rate. If the selected model misses the gate, swap to
the runner-up on the GPU host (`m1/serve.sh`) and rerun.

---

## Wrap-up state for module 3

`case-api` running; `taco-app-data` and `taco-case-data` seeded; `.env` and the secret in place;
model host untouched.

```bash
podman-compose ps
```
