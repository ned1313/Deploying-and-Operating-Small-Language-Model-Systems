# Architecture of the Course Demonstration

This document describes the Taco Alley complaint-resolution system as it grows module by module. Each
section shows what exists at the end of that module. Update it when a module is completed: change the
module's status, replace planned diagrams with what was built, and keep earlier sections accurate if
shared components change.

| Module | Status | Adds |
| --- | --- | --- |
| [1](#module-1-hosting-a-small-language-model) | Implemented | vLLM model server, nginx gateway, screening and load tools |
| [2](#module-2-building-an-slm-agent-with-tools) | Implemented | Shared dataset and package, mock case API, LangChain agent with typed tools, structured proposal and validator, agent evaluation |
| [3](#module-3-orchestrating-and-hardening-the-agent-workflow) | Implemented | LangGraph workflow in a complaint API, durable checkpoints, human approval, idempotent actions, failure handling |
| [4](#module-4-monitoring-and-tracing) | Implemented | Prometheus, Grafana, GPU/host exporters, structured logs, LangSmith tracing, Locust, performance baseline |
| [5](#module-5-quality-releases-and-cost) | Implemented (GPU host steps pending) | Quality sampling and scoring, model release and rollback, cost per successful resolution |
| [6](#module-6-security-and-auditing-planned) | Planned | TLS and service keys at the gateway, rate limits, authenticated metrics, user roles, audit store |

## System context

Two machines: a dedicated GPU host serves the model; the desktop runs the application, tooling, and
(from module 4) monitoring. A single machine can run both, using the same configuration contract.

```mermaid
flowchart LR
    subgraph Desktop["Desktop (Windows, Podman in WSL)"]
        Rep["Service representative"]
        App["taco-app project<br/>(module 2+)"]
        Obs["taco-observe project<br/>(module 4+)"]
    end
    subgraph GPU["GPU host (Ubuntu, RTX 3090, Podman)"]
        GW["nginx gateway :8080"]
        VLLM["vLLM model server"]
    end
    Rep --> App
    App -- "OpenAI-compatible HTTP<br/>INFERENCE_BASE_URL" --> GW
    GW --> VLLM
    Obs -. "scrape metrics" .-> GW
```

The inference connection contract shared by every client:

| Setting | Example | Source |
| --- | --- | --- |
| `INFERENCE_BASE_URL` | `http://<MODEL_HOST>:8080/v1` | `.env` |
| `INFERENCE_MODEL` | `qwen3-14b` | `.env` |
| `INFERENCE_EXTRA_BODY` | `{"chat_template_kwargs": {"enable_thinking": false}}` | `.env` |
| service credential | file mounted at `/run/secrets/inference_api_key` | `secrets/` (enforced from module 6) |

## Deployment units

Each unit is a top-level folder with one compose project. Modules add services to units; the module
folders (`m1/` ... `m6/`) hold only runbooks.

| Folder | Compose project | Host | Contents | From module |
| --- | --- | --- | --- | --- |
| `model-host/` | `taco-model` | GPU host | `vllm`, `gateway`; exporters | 1; exporters 4 |
| `app/` | `taco-app` | desktop | `case-api`, `complaint-api`; `seed`, `basic-agent`, `cli` one-shots | 2; `complaint-api` 3 |
| `observability/` | `taco-observe` | desktop | `prometheus`, `grafana` | 4 |
| `tools/` | `taco-tools` | either | one-shot evaluation, load, report, and test commands | 1 |
| `shared/` | - | - | `taco_shared` package and data used by `app/` and `tools/` | 1 |

Credentials are in `secrets/` and results in `results/<kind>/`, both git-ignored and per host.

---

## Module 1: Hosting a Small Language Model

The model host project (`taco-model`, `model-host/`) on the GPU host. vLLM is published only on
loopback; the gateway is the only path from the LAN. Disposable tool containers (`tools/`) run the
smoke test, screening, and load sweeps from either host through the gateway's published port.

```mermaid
flowchart LR
    subgraph GPU["GPU host"]
        direction LR
        subgraph Model["compose project taco-model"]
            subgraph Net["network taco-model-inference (10.89.7.0/24)"]
                GW["gateway<br/>nginx :8080<br/>unauthenticated"]
                VLLM["vllm<br/>fixed IP 10.89.7.10:8000<br/>--enable-auto-tool-choice<br/>--tool-call-parser"]
            end
        end
        Tools1["taco-tools (one-shot)<br/>smoke, screen,<br/>load-sweep, compare-*<br/>host.containers.internal:8080"]
        Cache[("volume hf-cache<br/>model weights")]
        GPUdev["RTX 3090 via CDI"]
        Results1[("results/serving, screening, sweep")]
    end
    subgraph Desktop["Desktop"]
        Tools1d["taco-tools (one-shot)<br/>INFERENCE_BASE_URL=http://MODEL_HOST:8080/v1"]
    end
    Tools1 --> GW --> VLLM
    Tools1d -- LAN --> GW
    VLLM --- Cache
    VLLM --- GPUdev
    Tools1 --> Results1
```

Screening sends each scenario with its order record, prior resolutions, and the policy inlined in
the prompt, and grades the JSON answer. No tools are involved.

```mermaid
flowchart LR
    Data["screening_scenarios.json<br/>policy 2026.09.txt<br/>proposal.v1.json"] --> Prompt["system prompt + inlined facts"]
    Prompt --> Model["vLLM chat completion<br/>(optional json_schema)"]
    Model --> Parse["strict JSON parse"]
    Parse --> Grade["grade(): schema, grounded,<br/>policy, category"]
    Grade --> Out[("screening-*.json")]
```

---

## Module 2: Building an SLM Agent with Tools

### Shared foundation

Code, data, and services used by more than one module moved into `shared/`. The complaint CSV is the
single source for customers, stores, and complaint text; a deterministic build synthesizes orders,
delivery times, prior resolutions, and cases, labels every row with a reference policy engine, and
renders the same curated scenarios two ways: facts inlined (module 1) and facts behind tools
(module 2).

```mermaid
flowchart TB
    CSV["source/taco_alley_customer_complaints.csv<br/>1,000 rows, deliberate defects"] --> Build["dataset/build.py<br/>clean, map categories,<br/>synthesize per-row (seeded RNG)"]
    Ref["menu, stores, category_map,<br/>curated.json (S01-S22, A01-A02)"] --> Build
    Policy["policy/2026.09.json<br/>R1-R14"] --> Engine["policy_engine.evaluate()"]
    Build --> Engine
    Engine --> Gen
    Build --> Gen[("generated/<br/>orders, customers, prior_resolutions,<br/>cases, complaints.jsonl")]
    Gen --> Screen["screening_scenarios.json<br/>(module 1)"]
    Gen --> AgentS["agent_scenarios.json<br/>(module 2)"]
    Gen --> Seed["dataset/seed.py"] --> AppDB[("SQLite taco_app.db")]
    Gen --> CaseDB[("SQLite cases.db<br/>seeded by case API")]
```

### Deployment

The application project (`taco-app`, `app/`) runs on the desktop and consumes the module 1 gateway;
it never starts or stops the model server. The agent evaluation is a one-shot in the tools project
(`taco-tools`) that joins the application's network and reads its seeded data. Each SQLite file has
exactly one owning service.

```mermaid
flowchart LR
    subgraph Desktop["Desktop"]
        subgraph AppP["compose project taco-app (network taco-app-private)"]
            Agent["basic-agent (one-shot)<br/>examples/basic_agent.py"]
            SeedJ["seed (one-shot)"]
            Case["case-api<br/>FastAPI :8081<br/>(127.0.0.1 only)"]
            AppVol[("volume taco-app-data<br/>taco_app.db")]
            CaseVol[("volume taco-case-data<br/>cases.db")]
        end
        subgraph ToolsP["compose project taco-tools"]
            Eval["agent-eval (one-shot)"]
            Cmp["agent-compare (one-shot)"]
        end
        Secret["secrets/inference_api_key"]
        Res[("results/agent, results/screening")]
    end
    subgraph GPU["GPU host: taco-model"]
        GW["gateway :8080"] --> VLLM["vllm"]
    end
    SeedJ -- write --> AppVol
    Agent -- read-only --> AppVol
    Eval -- read-only --> AppVol
    Case --- CaseVol
    Agent -- "X-Store-Scope" --> Case
    Eval -- "taco-app-private" --> Case
    Agent -- LAN --> GW
    Eval -- LAN --> GW
    Secret -.-> Agent
    Secret -.-> Eval
    Agent --> Res
    Eval --> Res
    Cmp -. "reads" .-> Res
```

### Agent design

The teaching script builds a LangChain `create_agent` agent with four read-only tools and drives it
with its own loop: it streams every step, then makes a separate structured-output call and validates
the proposal. Trusted context (actor, store scope, customer, case) is bound into the tools by the
application and never appears in the tool schemas.

```mermaid
flowchart TB
    Intake["intake: case_id, customer_id,<br/>complaint_date, complaint text<br/>(order id only inside the text)"]
    Ctx["AgentContext (trusted)<br/>actor, store_ids, customer, case"]
    subgraph Loop["shared/taco_shared/teaching_agent/loop.py"]
        CA["create_agent(ChatOpenAI, tools,<br/>ToolCallLimitMiddleware)"]
        Stream["stream_mode=updates<br/>render + transcript"]
        Final["finalize: with_structured_output<br/>(proposal.v2.json, method=json_schema)<br/>no tools in this call"]
        Val["validate_proposal()<br/>schema + evidence-based business rules"]
    end
    subgraph Tools["taco_shared.agent.tools (StructuredTool)"]
        T1["get_order(order_id)"]
        T2["get_prior_resolutions(lookback_days)"]
        T3["get_resolution_policy(complaint_category)"]
        T4["get_case_status()"]
    end
    Intake --> CA
    Ctx --> Tools
    CA <--> Tools
    CA --> Stream --> Final --> Val
    T1 --> DB[("taco_app.db<br/>scoped, read-only")]
    T2 --> DB
    T3 --> Pol["policy 2026.09"]
    T4 --> CaseAPI["case-api"]
    Val --> Outcome["ACCEPTED (pending approval in module 3)<br/>or REJECTED with violations"]
```

One investigation, as the model and tools exchange messages:

```mermaid
sequenceDiagram
    participant S as basic_agent.py (driver loop)
    participant A as create_agent graph
    participant M as vLLM (via gateway)
    participant T as Tools
    participant V as validate_proposal
    S->>A: stream(intake)
    loop until no tool calls or limit reached
        A->>M: chat completion with tool specs
        M-->>A: AIMessage.tool_calls (parsed by --tool-call-parser)
        A->>T: invoke with model-chosen args + bound context
        T-->>A: ToolMessage (JSON with record_id or typed error)
        A-->>S: step update (rendered)
    end
    S->>M: finalize: history + response_format json_schema (no tools)
    M-->>S: proposal JSON
    S->>V: proposal + evidence from tool results
    V-->>S: schema errors, business-rule violations
```

What the module 2 loop intentionally lacks (added in module 3): durable state, custom routing for
clarification and escalation, retry budgets, and a human-approval pause before any action.

---

## Module 3: Orchestrating and Hardening the Agent Workflow

The complaint API (`app/complaint_api/`) joins the `taco-app` project. It runs an explicit LangGraph
`StateGraph` from `shared/taco_shared/workflow`, checkpointed with `SqliteSaver`, and reuses the module
2 model factory, tools, prompts, proposal call, and validator. The model chooses tools; application
code chooses every route.

### Deployment

```mermaid
flowchart LR
    subgraph Desktop["Desktop: compose projects taco-app (network taco-app-private) and taco-tools"]
        CLI["cli (taco-app) /<br/>failure-tests, e2e-eval (taco-tools)<br/>(one-shot)"]
        UI["browser: /ui<br/>(127.0.0.1:8090)"]
        API["complaint-api<br/>FastAPI :8090, one worker"]
        Case["case-api :8081<br/>actions + admin faults"]
        AppVol[("taco-app-data<br/>taco_app.db (read-only)")]
        StateVol[("taco-app-state<br/>checkpoints.db, app.db")]
        CaseVol[("taco-case-data<br/>cases.db")]
        Secret["secrets/inference_api_key"]
    end
    subgraph GPU["GPU host: taco-model"]
        GW["gateway :8080"] --> VLLM["vllm"]
    end
    CLI -- "X-Actor-Id" --> API
    UI -- "?actor=" --> API
    API -- read --> AppVol
    API --- StateVol
    API -- "X-Store-Scope,<br/>Idempotency-Key" --> Case
    Case --- CaseVol
    API -- "service key (LAN)" --> GW
    Secret -.-> API
    CLI -. "admin token" .-> Case
```

Two identities: the service key authenticates the application to the inference gateway (module 6
enforces it); `X-Actor-Id` names the person using the complaint API, from which the API derives role
(service_rep, reviewer, area_manager) and store scope. The header is an unauthenticated claim, so the
API is published on loopback only.

### Workflow

```mermaid
flowchart TB
    Start((submit)) --> Intake["intake<br/>order-id precheck"]
    Intake -->|no order id| Clarify["clarify (end)"]
    Intake --> Inv["investigate<br/>model + bound tools"]
    Inv -->|tool calls| Tools["tools<br/>repeat guard, budget,<br/>case API retries"]
    Tools --> Inv
    Inv --> Check["check_evidence"]
    Tools -->|budget spent| Check
    Check -->|order not_found| Clarify
    Check -->|case API unavailable| Esc["escalate (end)"]
    Check -->|tool missing, 1 nudge| Inv
    Check -->|still missing| Esc
    Check --> Propose["propose<br/>json_schema, no tools"]
    Propose --> Val["validate"]
    Val -->|invalid, 1 correction| Propose
    Val -->|invalid again| Esc
    Val -->|accepted| Review["human_review<br/>interrupt(), checkpointed"]
    Review -->|approve, current version| Record["record<br/>Idempotency-Key case:vN"]
    Review -->|revise + note| Propose
    Review -->|reject| Rejected["rejected (end)"]
    Record --> Done((recorded))
```

An unreachable inference endpoint (after the client's retries) or case API at record time raises out
of the graph: the case becomes `failed_retryable` and keeps its checkpoint, and `retry` resumes the
failed node. On startup the API resumes runs that were cut off mid-investigation.

## Module 4: Monitoring and Tracing

An independent `taco-observe` project (`observability/`) on the desktop with Prometheus and Grafana,
one-shot Locust and baseline runs in the tools project, and exporters (node-exporter, dcgm-exporter or
the nvidia-smi fallback) added to the `taco-model` project on the GPU host. The application's metrics,
JSON logs, and LangSmith hooks are in `app/complaint_api/`. Prometheus scrapes vLLM's `/metrics` through the gateway
without authentication; module 6 moves that job behind a credential with the same job and instance
labels.

```mermaid
flowchart LR
    subgraph Desktop["Desktop"]
        subgraph App["taco-app (app/)"]
            API["complaint-api<br/>/metrics, JSON logs,<br/>run slots (4)"]
            Case["case-api"]
        end
        subgraph Obs["taco-observe (observability/)"]
            Prom["prometheus :9090<br/>rules + alerts"]
            Graf["grafana :3000<br/>3 dashboards"]
        end
        subgraph ToolsP["taco-tools (tools/)"]
            Load["load (one-shot)<br/>Locust direct | app"]
            Base["baseline (one-shot)"]
        end
        Tel{{"network taco-telemetry"}}
    end
    subgraph GPU["GPU host"]
        GW["gateway :8080<br/>JSON log, X-Request-ID"] --> VLLM["vllm"]
        Node["node-exporter :9100"]
        Dcgm["dcgm-exporter :9400"]
    end
    API --- Tel --- Prom
    Prom --> Graf
    Base -- "taco-telemetry" --> Prom
    Load -- "taco-app-private" --> API
    Load -- "direct mode (LAN)" --> GW
    API -- "X-Request-ID (LAN)" --> GW
    API --> Case
    Prom -. "vllm job" .-> GW
    Prom -.-> Node
    Prom -.-> Dcgm
    API -. "traces" .-> LS["LangSmith (hosted)"]
```

Correlation: the complaint API accepts or generates a request id, returns it, logs it on every JSON
line (`model_call`, `tool_call`, `workflow_run`, `http_request`), forwards it to the gateway on every
model call, and records it as LangSmith metadata next to `case_id`, `thread_id`, and the deployment
versions (`taco_build_info`). A case produces one trace per graph invocation (`submit`, `decision`,
`clarify`, `retry`), grouped by `thread_id`; approval wait is a metric, not a span.

## Module 5: Quality, Releases, and Cost

The complaint API samples completed workflow outcomes into an evaluation store (`quality.db` on the
`taco-app-quality` volume). One-shot tools score them, gate releases, and compute cost per successful
resolution. Every release is served under the stable alias `taco-slm`, so a release or rollback is a
model-host action.

```mermaid
flowchart LR
    subgraph Desktop["Desktop"]
        subgraph App["taco-app (app/)"]
            API["complaint-api<br/>samples outcomes<br/>/ui/quality, taco_quality_*"]
            QVol[("taco-app-quality<br/>quality.db: samples,<br/>scores, reviews")]
        end
        subgraph ToolsP["taco-tools (tools/)"]
            Q["quality (one-shot)<br/>checks, labels, flags,<br/>queue, review"]
            Rel["release (one-shot)<br/>gates vs baseline"]
            Cost["cost (one-shot)"]
            Eval["e2e-eval, failure-tests,<br/>load, baseline<br/>--label release id"]
        end
        subgraph Obs["taco-observe"]
            Prom["prometheus<br/>QualityDegraded"] --> Graf["grafana<br/>Quality and cost"]
        end
        Res[("results/quality, release, cost")]
    end
    subgraph GPU["GPU host: taco-model"]
        RelSh["release.sh apply | rollback<br/>releases/*.json"] --> VLLM["vllm<br/>served as taco-slm + real name"]
    end
    API -- write samples --> QVol
    Q -- write scores --> QVol
    API -. "/metrics" .-> Prom
    Cost --> QVol
    Cost --> Prom
    Eval --> API
    Eval --> Res
    Rel --> Res
    Q --> Res
    API -- "model taco-slm" --> VLLM
```

Scoring per sample: deterministic checks recomputed from the evidence the tools returned (amount for the
cited rule, rule fits the category, approval flag both ways, filing window, prior compensation, duplicate
actions, policy version, schema), `grade()` against a label where the case is synthetic and labeled, and
human rubric reviews. A sample is flagged on any failure; a model is degraded when its last 20 scored
samples fall below the thresholds in `tools/quality-gates.yaml`. A release is promoted only if the
candidate's labeled runs pass the gates in the same file against the baseline's.

## Module 6: Security and Auditing (planned)

The gateway gains TLS, per-service bearer keys (the module 2 secret file starts being enforced), key
rotation, route restrictions, request-size and rate limits, and an authenticated metrics route that
Prometheus switches to. The application enforces user roles and store scope (already in the tools) and
writes protected audit records separate from operational logs and quality samples.
