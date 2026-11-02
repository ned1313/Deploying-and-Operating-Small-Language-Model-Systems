# Demo 3.1: Orchestrate, validate, and approve a resolution

Goal: run the module 2 complaint through a LangGraph workflow inside the complaint API. Validate the
proposal, pause at a durable approval checkpoint, restart the API container, approve as an authorized
reviewer, record the action once, and show that retries do not duplicate it.

Prerequisites: module 1 model and gateway running on the GPU host, and the setup in
[app/README.md](../app/README.md#setup) (`.env`, secret, `build`, `seed`, `case-api` and
`complaint-api` up). Commands run from `app/`.

Before you start, and whenever you rerun the demo, return the demo case to its fixture:

```bash
podman-compose run --rm cli reset CASE-10431
```

---

## Clip 1: adding orchestration with LangGraph

### 1. Start the complaint API and check its dependencies

```bash
podman-compose up -d case-api complaint-api
curl -s http://127.0.0.1:8090/healthz | python3 -m json.tool
```

```json
{"status": "ok", "model": "qwen3-14b",
 "dependencies": {"case_api": true, "inference": {"reachable": true, "status_code": 200, "model_served": true}}}
```

Key point: liveness is "ok" whenever the API serves requests; the remote inference endpoint and
the case API are reported separately, so an outage on the GPU host never turns into a restart loop.

### 2. Map the module 2 loop onto the graph

```bash
podman-compose run --rm cli graph
```

The output is a Mermaid diagram of the graph; paste it into any Mermaid viewer to see it rendered.
How the module 2 loop maps onto the graph:

| Module 2 (`shared/taco_shared/teaching_agent/loop.py`) | Module 3 node |
| --- | --- |
| `create_agent` model turn | `investigate` |
| `create_agent` tool node | `tools` (adds a repeated-call guard and the tool-call budget) |
| (nothing) | `check_evidence`: deterministic routing to clarify, escalate, or one nudge for missing tools |
| `finalize()` | `propose` (same shared proposal call) |
| `validate_proposal()` | `validate` (one correction attempt, then escalate) |
| (nothing) | `human_review` (`interrupt()`), `record` (idempotent), `clarify`, `escalate`, `rejected` |

Read through `shared/taco_shared/workflow/graph.py`: the state (`state.py`), each node, and the
`route` function that reads `next_step`. The model chooses tools; application code chooses routes.

### 3. Submit the missing-items complaint

```bash
podman-compose run --rm cli submit A01
```

```
POST /complaints as rep-kop-01 -> HTTP 201
Case CASE-10431  status: pending_review  store KOP  submitted by rep-kop-01
  complaint: Two of the three carnitas tacos I ordered were missing from my delivery bag. ... Order TA-10431.
  proposal v1: partial_refund 8.50  order TA-10431  items ['Carnitas Taco']  rules ['R1']  approval flag False
  validation: accepted
  counters: tool_calls=4  repeated_calls=0  nudges=0  corrections=0  case_api_retries=0  model_calls=6
```

`--steps` on `cli show CASE-10431` prints every node's step. Notice that the run
stopped at `human_review`: the workflow state is in `checkpoints.db` on the `taco-app-state` volume,
not in memory.

### 4. Restart the API while the proposal waits

```bash
podman restart taco-app_complaint-api_1
podman-compose run --rm cli reviews
```

```text
GET /reviews as sup-kop-01 -> HTTP 200
  CASE-10431         pending_review       store KOP  v1  by rep-kop-01
```

### 5. Approve as an authorized reviewer

Identity is the `X-Actor-Id` header (an unauthenticated claim; the API is on loopback only). It is
separate from the inference service key that module 6 enforces at the gateway.

```bash
podman-compose run --rm cli --actor rep-kop-01 approve CASE-10431     # 403: only reviewers decide
podman-compose run --rm cli --actor sup-wg-01 approve CASE-10431      # 404: not in the reviewer's stores
podman-compose run --rm cli --actor sup-kop-01 approve CASE-10431                        # sup-kop-01
```

```text
POST /reviews/CASE-10431/decision as sup-kop-01 -> HTTP 200
Case CASE-10431  status: recorded ...
  review: v1 approve by sup-kop-01
  recorded action: APR-901ea6f1b8 partial_refund 8.50 at 2026-10-09T18:52:01+00:00
```

Alternatively, use the review page at `http://127.0.0.1:8090/ui?actor=sup-kop-01`: it shows the
same proposal, validation, and Approve / Request revision / Reject buttons, plus a **Workflow steps**
table that is easier to read than `cli show --steps`. After approval, the case moves to the
"recorded" tab of the list. With Podman in WSL and the
browser on Windows, see "Browser on Windows, Podman in WSL" in [app/README.md](../app/README.md#browser-on-windows-podman-in-wsl) if the page does
not open.

### 6. Retries do not duplicate the action

```bash
podman-compose run --rm cli approve CASE-10431           # same decision again
podman-compose run --rm cli submit A01                   # same complaint again
curl -s -H 'X-Store-Scope: KOP' http://127.0.0.1:8081/cases/CASE-10431 | python3 -m json.tool
```

Both calls return `[replayed: no new run]`, and the case API shows one recorded action. Two layers:
the complaint API returns the existing case for a known `case_id`, and the record step sends
`Idempotency-Key: CASE-10431:v1`, so even a retried POST after a timeout records the action once.
A different key on a case that already has a monetary action gets `409` (the R13 recheck at record
time).

### 7. Optional: request a revision

```bash
podman-compose run --rm cli reset CASE-10431
podman-compose run --rm cli submit A01
podman-compose run --rm cli revise CASE-10431 --note "Offer a replacement instead of a refund."
podman-compose run --rm cli approve CASE-10431 --version 1      # 409: version 1 is stale
podman-compose run --rm cli approve CASE-10431
```

Approval is bound to the exact proposal version; each revision is validated again and needs a fresh
decision.

---

## Wrap-up state for Demo 3.2

`case-api` and `complaint-api` running; `CASE-10431` recorded. Demo 3.2 resets the cases it uses.
Before rerunning the module 2 demo, restore A01's fixture: `podman-compose run --rm cli reset CASE-10431`
(a recorded action makes module 2 see the order as already compensated).
