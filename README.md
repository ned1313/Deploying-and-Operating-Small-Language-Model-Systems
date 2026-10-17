# Deploying and Operating Small Language Model Systems

These are the exercise files meant to accompany my Pluralsight course of the same name. In the course, you are challenged with the task of using local hardware to run SLMs, attach the SLMs to an agentic workflow, and monitor that workflow for optimization and performance.

The running example is Taco Alley, a fictional restaurant chain whose customer complaints are resolved
by an agent: a small language model investigates each complaint with tools, proposes a resolution, and
a person approves it before anything is recorded. Every customer, order, and complaint is synthetic.

## How the repository is organized

The repository is split two ways:

- **Deployment units** (`model-host/`, `app/`, `observability/`, `tools/`) are the things you run.
  Each is a top-level folder with one Podman Compose project, an `.env.example` (where settings are
  needed), and a README describing its services, setup, and teardown. Later modules add services and
  settings to these same folders rather than creating new copies.
- **Module folders** (`m1/` ... `m6/`) contain no code. Each holds a README (what the module covers and
  which units it uses) and one runbook per demo (`demo-<module>.<n>-*.md`) with the commands to follow
  along with the video.

Supporting folders hold the shared Python package (`shared/`), helper scripts (`scripts/`), and the
git-ignored locations for credentials (`secrets/`) and generated output (`results/`).

```text
.
├── README.md                 this file
├── ARCHITECTURE.md           how the system grows module by module, with diagrams
├── m1/ ... m6/               module READMEs and demo runbooks (start here)
│
├── model-host/               GPU host: compose project taco-model
│   ├── compose.yaml          vllm, gateway, exporters; gateway-secure and step-ca (module 6)
│   ├── env.sh                host settings, pinned images and model revisions, the `tools` shell function
│   ├── serve.sh              start vLLM with a chosen candidate model
│   ├── release.sh            apply, roll back, and inspect model releases (module 5)
│   ├── releases/             release manifests
│   ├── gateway/              nginx configs (plain in modules 1-5, TLS in module 6)
│   ├── pki/                  course certificate authority scripts (module 6)
│   └── exporters/            GPU exporter counter list
│
├── app/                      desktop application: compose project taco-app
│   ├── compose.yaml          case-api, complaint-api, authelia, app-gateway; seed, cli, basic-agent, reset one-shots
│   ├── complaint_api/        FastAPI service running the LangGraph workflow, review UI, metrics, audit
│   ├── case_api/             mock case-management API
│   ├── clients/              the `cli` used throughout modules 3-6, and the demo reset
│   ├── examples/             the module 2 teaching script (basic_agent.py)
│   ├── authelia/, gateway/   sign-in and TLS front end (module 6)
│   └── tests/
│
├── observability/            desktop monitoring: compose project taco-observe
│   ├── prometheus/           scrape configs, recording and alerting rules
│   └── grafana/              provisioning and generated dashboards
│
├── tools/                    disposable one-shot commands: compose project taco-tools
│   ├── taco_tools/           serving, agent_eval, e2e, load, quality, reports, security
│   ├── quality-gates.yaml    quality and release thresholds (module 5)
│   ├── rubric.md             human review rubric (module 5)
│   └── tests/
│
├── shared/                   the taco_shared Python package used by app/ and tools/
│   ├── taco_shared/          agent tools, workflow, prompts, validator, dataset, quality, audit, TLS
│   ├── taco_shared/data/     complaint dataset, policy, schemas, actors, generated scenarios
│   ├── requirements/         pinned dependencies for the app and tools images
│   └── tests/
│
├── scripts/                  desktop helpers for module 6: issue-certs.sh, setup-app-auth.sh
├── secrets/                  credential files (git-ignored; see secrets/README.md)
└── results/                  generated results, one subfolder per kind (git-ignored)
```

| Folder | Runs on | Contents |
| --- | --- | --- |
| [model-host/](model-host/README.md) | GPU host | vLLM, the nginx gateway, and host/GPU exporters (project `taco-model`), plus `env.sh`, `serve.sh`, and `release.sh` |
| [app/](app/README.md) | desktop | The complaint API (LangGraph workflow, review page), the mock case API, the CLI, sign-in and audit from module 6, and the module 2 teaching script (project `taco-app`) |
| [observability/](observability/README.md) | desktop | Prometheus and Grafana (project `taco-observe`) |
| [tools/](tools/README.md) | either | Disposable evaluation, load, report, quality, release, and test commands (project `taco-tools`) |
| [shared/](shared/README.md) | - | The `taco_shared` Python package used by `app/` and `tools/`: the complaint dataset and its generated orders and scenarios, the resolution policy, proposal schemas, agent tools, the workflow, quality checks, and audit |
| `scripts/` | desktop | Module 6 helpers: `issue-certs.sh` (certificates and rotation) and `setup-app-auth.sh` (Authelia users and application secrets) |
| [secrets/](secrets/README.md) | each host | Git-ignored credential files; the README lists every file, who reads it, and how it is created |
| `results/` | each host | Git-ignored output, e.g. `results/screening/`, `results/agent/`, `results/load/` |

## The modules

| Module | Runbooks | What you build |
| --- | --- | --- |
| [m1/](m1/README.md) | 1.1-1.3 | Serve a quantized model with vLLM behind an nginx gateway, screen three candidates, and tune serving under load |
| [m2/](m2/README.md) | 2.1 | A LangChain agent with typed tools that investigates a complaint and produces a validated, structured resolution proposal |
| [m3/](m3/README.md) | 3.1-3.2 | A complaint API running a LangGraph workflow with durable approval checkpoints, bounded retries, clarification and escalation routes, idempotent recording, and failure tests |
| [m4/](m4/README.md) | 4.1-4.2 | Prometheus, Grafana, and GPU/host exporters across both hosts, correlated structured logs, LangSmith traces, Locust load generation, and a documented performance baseline |
| [m5/](m5/README.md) | 5.1-5.3 | Sampled output quality with deterministic checks and a review queue, pinned model releases with gates and rollback, and cost per successful resolution |
| [m6/](m6/README.md) | 6.1-6.2 | A TLS model gateway with per-service client certificates from a course CA (step-ca), authenticated metrics, Authelia sign-in for people, and an encrypted, tamper-evident audit trail |

The modules build on each other: each one starts from the state the previous one left running. See
[ARCHITECTURE.md](ARCHITECTURE.md) for a diagram of the system at the end of each module.

## Following along

1. Start with [m1/README.md](m1/README.md), then work through its runbooks in order.
2. Run each unit's commands from its own folder (`cd app && podman-compose ...`). The runbooks say which
   folder and which host each step uses.
3. On the GPU host, run `source model-host/env.sh` once per shell. It sets `COMPOSE_FILE` so plain
   `podman compose ...` targets the model host project, and defines `tools` for running tools-project
   commands from any directory.
4. Copy each unit's `.env.example` to `.env` when its README tells you to. `.env` files, `secrets/`,
   `results/`, `model-host/secrets/`, and `model-host/local.env` are git-ignored, so your settings and
   credentials never end up in a commit.
5. Stop services with `podman-compose stop` (or `podman compose stop` on the GPU host). Avoid
   `down --volumes`: named volumes hold the seeded data, workflow state, quality samples, and audit trail
   that later modules rely on.

## Course Prerequisites

The course assumes that you have access to a system that can run the following:

- Podman with `podman compose` on the GPU host, and `podman-compose` on the desktop (the runbooks were
  tested with Podman 4.9.3 and podman-compose 1.0.6 under WSL)
- NVIDIA Container Toolkit (CDI) for GPU access from containers
- Physical GPU with at least 16GB VRAM
- A free [LangSmith](https://smith.langchain.com) account for the tracing demos in modules 4 and 6 (optional)

No local Python installation is needed: every component runs in a container.

For the demonstrations, I am using a dedicated machine for model hosting. The agentic application is running on a separate system. I chose to do this to keep the model from interfering with my recording software. You can run both the agentic application and model on the same system. The system running the model software is an Ubuntu 26.04 LTS box with an Nvidia GeForce RTX 3090 card.

I chose to run vLLM in a container because I think it is simpler and cleaner to have an isolated inference runtime. There are just too many dependencies, drivers, and middleware that would need to be installed locally. I'd rather not. You can choose to run the model however you'd like. You just need a valid OpenAI-compatible API endpoint for model serving and a metrics endpoint to hook into the observability stack.
