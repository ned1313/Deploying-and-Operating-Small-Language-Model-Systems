# Repository layout migration plan

Status: applied on the desktop (2026-10-14), not committed. The GPU host steps are still to run (section
6). Decisions are recorded in section 3; what was built and how it differs from the plan is in
section 6.

Goal: move every runnable component out of the module folders into top-level folders, one per
deployment unit, each with the same set of files. Module folders keep only their README and
runbooks (`demo-*.md`).

---

## 1. Current layout and the problems with it

| Folder | Runs on | Compose project | What it actually contains |
| --- | --- | --- | --- |
| `m1/` | GPU host | `taco-m1` | vLLM, nginx gateway, `env.sh`, `serve.sh`, `tools` image (smoke, screening, load sweep, compare) |
| `m2/` | desktop | `taco-app` | case API, `seed`, `basic-agent`, `agent-eval`, `agent-compare`, `tests`; image `taco-m2-agent` |
| `m3/` | desktop | `taco-app` (superset of m2) | case API, complaint API, `seed`, `cli`, `failure-tests`, `e2e-eval`, `e2e-compare`, `tests`; image `taco-m3-app` |
| `m4/` | desktop | `taco-observe` | Prometheus, Grafana, `load` and `baseline` one-shots; image `taco-m4-load` |
| `m4/model-host/` | GPU host | `taco-model-exporters` | node-exporter, dcgm-exporter, nvidia-smi exporter |
| `shared/` | n/a | n/a | `taco_shared` package, data, requirements, mock case API source, shared tests |

Problems:

1. **Two compose files define the same project.** `m2/compose.yaml` and `m3/compose.yaml` are both
   `taco-app`, with the same network and volumes but different images. Running one after the other
   recreates services with a different image.
2. **Where you run a command depends on the module you are in, not on what you are running.** The
   complaint API is started from `m3/` in modules 3-6; monitoring from `m4/`; module 5 would add a
   third place for evaluation one-shots.
3. **Later modules edit earlier modules' folders.** Module 4 changed `m3/app`, `m3/compose.yaml`,
   and `m1/gateway/nginx.conf`; module 5 and 6 would keep doing that.
4. **Configuration is duplicated and cross-linked.** `INFERENCE_MODEL`, `INFERENCE_EXTRA_BODY`, and
   `TACO_ADMIN_TOKEN` are in both `m3/.env` and `m4/.env`; the complaint API reads LangSmith settings
   from `../m4/.env`; `m4/compose.yaml` mounts `../m3/secrets/inference_api_key`; the m3 setup copies
   the key from `../m2/secrets/`.
5. **One-shots are scattered across four images.** `taco-m1-tools`, `taco-m2-agent`, `taco-m3-app`,
   and `taco-m4-load` each carry part of the evaluation tooling, with overlapping requirements.
6. **Results are spread across module folders** and compare scripts reach across them
   (`M1_RESULTS_DIR=../m1/results`, `M2_RESULTS_DIR=../m2/results`).
7. **Python package names collide.** `m2/eval` and `m3/eval` are both the top-level package `eval`
   (which also shadows a builtin name), and `m3/app` is the package `app`.
8. **The GPU host has two projects** (`taco-m1`, `taco-model-exporters`) for one deployment unit.

---

## 2. Proposed target layout

Folders follow the deployment units in the outline: model host, application, observability, and
disposable tools (evaluation and load). Each unit folder has the same shape:

```
<unit>/
  README.md          what it runs, setup, commands, ports, volumes
  compose.yaml       one compose project per unit
  .env.example       settings this unit reads (copied to a git-ignored .env)
  Containerfile(s)   images built by this unit (if any)
  <source dirs>
  tests/             tests for this unit's code (if any)
```

```
model-host/                      GPU host, project taco-model
  README.md
  compose.yaml                   vllm, gateway, node-exporter, dcgm-exporter, nvidia-smi-exporter
  env.sh                         was m1/env.sh
  serve.sh                       was m1/serve.sh
  gateway/nginx.conf             was m1/gateway/nginx.conf
  exporters/dcgm-counters.csv    was m4/model-host/dcgm-counters.csv
  release.sh                     module 5 (planned)

app/                             desktop, project taco-app
  README.md
  compose.yaml                   case-api, complaint-api, seed, cli, basic-agent
  Containerfile                  one image: complaint API, CLI, teaching script
  .env.example                   inference contract, workflow budgets, ports, LangSmith
  tracing-off.env
  complaint_api/                 was m3/app (package renamed, Q5)
  case_api/                      was shared/services/mock_case_api (Q6)
  clients/                       was m3/clients (CLI and API client)
  examples/basic_agent.py        was m2/examples
  teaching_agent/                was m2/agent (package renamed, Q5)
  tests/                         was m2/tests + m3/tests (API, harness-independent tests)

observability/                   desktop, project taco-observe
  README.md
  compose.yaml                   prometheus, grafana
  .env.example                   MODEL_HOST, exporter ports, Grafana ports
  prometheus/                    was m4/prometheus
  grafana/                       was m4/grafana

tools/                           either host, project taco-tools (Q3)
  README.md
  compose.yaml                   one-shots only: smoke, screen, agent-eval, e2e-eval, failure-tests,
                                 compare, load, baseline; module 5 adds quality, release, cost
  Containerfile                  one tools image (Q4)
  .env.example                   target URLs, EXECUTION_HOST, load defaults
  taco_tools/                    package (Q5)
    serving/                     was m1/tools: smoke_test, screen_models, load_sweep, compare_*
    agent_eval/                  was m2/eval
    e2e/                         was m3/eval
    load/                        was m4/load
    reports/                     was m4/eval/baseline_report.py
  sample_vram.sh                 was m1/tools/sample_vram.sh (runs on the GPU host, not in a container)
  tests/

shared/                          unchanged role: taco_shared package, data, requirements, shared tests
secrets/                         git-ignored, one place for credential files (Q7)
results/                         git-ignored, one place for result files (Q8)

m1/ ... m6/                      README.md (module overview, links) + demo-*.md runbooks only
Outline.md, ARCHITECTURE.md, README.md, MIGRATION.md
```

### File mapping (old → new)

| Old | New |
| --- | --- |
| `m1/compose.yaml` (`vllm`, `gateway`) | `model-host/compose.yaml` |
| `m1/compose.yaml` (`tools`) | `tools/compose.yaml` (`smoke`, `screen`, `load-sweep`, `compare-*`) |
| `m1/env.sh`, `m1/serve.sh`, `m1/gateway/` | `model-host/` |
| `m1/tools/*.py`, `m1/tools/Containerfile` | `tools/taco_tools/serving/`, `tools/Containerfile` |
| `m1/tools/sample_vram.sh` | `tools/sample_vram.sh` (or `model-host/`, it runs on the GPU host) |
| `m2/compose.yaml` | merged into `app/compose.yaml` (`seed`, `basic-agent`) and `tools/compose.yaml` (`agent-eval`, `agent-compare`) |
| `m2/Containerfile` | merged into `app/Containerfile` and `tools/Containerfile` |
| `m2/agent/`, `m2/examples/` | `app/teaching_agent/`, `app/examples/` |
| `m2/eval/` | `tools/taco_tools/agent_eval/` |
| `m2/tests/` | `app/tests/` and `tools/tests/` (split by what they import) |
| `m2/.env.example`, `m2/secrets/README.md` | `app/.env.example`, `secrets/README.md` |
| `m3/compose.yaml` | `app/compose.yaml` (`case-api`, `complaint-api`, `seed`, `cli`) and `tools/compose.yaml` (`failure-tests`, `e2e-eval`, `e2e-compare`) |
| `m3/Containerfile` | `app/Containerfile` |
| `m3/app/`, `m3/clients/` | `app/complaint_api/`, `app/clients/` |
| `m3/eval/` | `tools/taco_tools/e2e/` |
| `m3/tests/` | `app/tests/` and `tools/tests/` |
| `m3/.env.example`, `m3/tracing-off.env`, `m3/secrets/README.md` | `app/`, `secrets/README.md` |
| `m4/compose.yaml` (`prometheus`, `grafana`) | `observability/compose.yaml` |
| `m4/compose.yaml` (`load`, `baseline`) | `tools/compose.yaml` |
| `m4/prometheus/`, `m4/grafana/` | `observability/` |
| `m4/load/`, `m4/eval/` | `tools/taco_tools/load/`, `tools/taco_tools/reports/` |
| `m4/model-host/` | `model-host/` |
| `m4/.env.example` | split: `observability/.env.example`, `tools/.env.example`, LangSmith block into `app/.env.example` |
| `m4/secrets/README.md` | `secrets/README.md` |
| `shared/services/mock_case_api/` | `app/case_api/` (Q6) |
| `m*/results/` | `results/` (Q8) |

### Names that change and names that stay

| Kind | Before | After | Effect |
| --- | --- | --- | --- |
| Project (GPU host) | `taco-m1`, `taco-model-exporters` | `taco-model` | containers recreated once; `vllm`, `gateway` keep their fixed `container_name` |
| Network (GPU host) | `taco-m1_inference` (10.89.7.0/24) | `taco-model-inference` (named explicitly, same subnet) | old network must be removed first: same subnet |
| Volume (GPU host) | `hf-cache` | `hf-cache` | unchanged, no model re-download |
| Project (desktop) | `taco-app`, `taco-observe` | unchanged | |
| Networks (desktop) | `taco-app-private`, `taco-observe-private`, `taco-telemetry` | unchanged | |
| Volumes (desktop) | `taco-app-data`, `taco-app-state`, `taco-case-data`, `taco-observe-prometheus`, `taco-observe-grafana` | unchanged | case data, checkpoints, and monitoring history survive |
| Images | `taco-m1-tools`, `taco-m2-agent`, `taco-m3-app`, `taco-m4-load`, `taco-case-api` | `taco-app`, `taco-case-api`, `taco-tools` (Q4) | old images removed after verification |
| Prometheus job/instance labels | `complaint-api`, `vllm`/`model-host`, `node`, `gpu` | unchanged | dashboards keep working |

---

## 3. Questions

Write your answer on the **DECISION** line under each item. The plan assumes the recommendation.

1. **Top-level folder names.** Proposed `model-host/`, `app/`, `observability/`, `tools/`, keeping
   `shared/`. Alternatives: match the compose project names (`taco-model/`, `taco-app/`,
   `taco-observe/`, `taco-tools/`), or group them under one parent (`deploy/model-host/`,
   `deploy/app/`, ...). Note that a top-level `app/` folder and the current `app` Python package
   would be confusing together, which is one reason for Q5.
   **DECISION**: Go with proposed layout

2. **Compose file naming.** (a) **`<unit>/compose.yaml`, run from inside the unit folder**
   (recommended: same convention as today, relative paths and `.env` resolve from the folder, and
   podman-compose 1.0.6 needs no extra flags). (b) Root-level files as the outline proposes
   (`compose.app.yaml`, `compose.observability.yaml`, `compose.model.yaml`, run with `-f` from the
   root). Option (b) means every relative path and `.env` lookup must be checked against
   podman-compose 1.0.6 behaviour, and the root becomes crowded. If (a), I would update the outline's
   proposed command flow to match.
   **DECISION**: **a**

3. **Where one-shots live.** (a) **Operational one-shots that act on the app (`seed`, `cli`,
   `basic-agent`) stay in `app/`; evaluation, load, and report one-shots go to a separate `tools/`
   project** that joins `taco-app-private` and `taco-telemetry` as external networks (recommended:
   matches the outline's separate "evaluation container" and "load-generation container" units, and
   `tools` can run on either host). (b) Everything that needs the app (including evaluation) in
   `app/`, load and reports in `observability/`. Fewer projects, but brings back the "which folder
   is this in" problem. (c) `cli` also moves to `tools/`.
   **DECISION**: **a**

4. **Images for the tools project.** Today the m1 tools image installs only `core.txt` (it also runs
   on the GPU host), the load image installs `load.txt`, and the m2/m3 images install `dev.txt`.
   (a) **One `taco-tools` image with `core + agent + load` requirements** (recommended: one build,
   one entrypoint convention; the image is larger but still CPU-only). (b) Two images: `taco-tools`
   (screening, evaluation, reports) and `taco-load` (Locust). (c) Keep one image per old module.
   **DECISION**: **a** less images is better.

5. **Python package names.** Proposed renames so nothing collides and nothing is named `app` or
   `eval`: `m3/app` → `complaint_api`, `m2/agent` → `teaching_agent`, tools code under one package
   `taco_tools` (`serving`, `agent_eval`, `e2e`, `load`, `reports`), run as
   `python -m taco_tools.e2e.run_e2e_scenarios` instead of `python eval/run_e2e_scenarios.py`. Imports
   in tests and scripts change accordingly; behaviour does not. Alternatively keep script-style entry
   points with `sys.path` inserts as today. OK with the renames?
   **DECISION**: OK with proposed renames.

6. **The mock case API.** It runs only in `taco-app`, but its source is in
   `shared/services/mock_case_api` and its tests are in `shared/tests/test_services.py`.
   (a) **Move it to `app/case_api/`** with its Containerfile and tests (recommended: "a unit folder
   contains everything its compose file builds"). (b) Leave it in `shared/` and build it from
   `app/compose.yaml` as today.
   **DECISION**: **a**

7. **Secrets location.** The inference key is used by the app, the tools, and load; the Grafana
   password by observability; module 6 adds service keys, a metrics credential, and TLS material.
   (a) **One git-ignored root `secrets/` folder** referenced as `../secrets/<name>` from every compose
   file (recommended: no copying between folders, one README listing every file and who reads it).
   (b) One `secrets/` folder per unit, with the inference key copied into each that needs it (as
   today). (c) (a) on the desktop, plus `model-host/secrets/` on the GPU host for module 6 gateway
   material, since the two hosts never share a checkout path. I lean toward (c).
   **DECISION**: **a**

8. **Results location.** Today: `m1/results`, `m2/results`, `m3/results`, `m4/results`, with
   recorded runs that runbooks and compare commands refer to.
   (a) **One git-ignored root `results/` with subfolders by kind** (`screening/`, `agent/`, `e2e/`,
   `failure/`, `load/`, `baseline/`, later `quality/`, `release/`, `cost/`), mounted into the tools
   container (recommended: compare commands no longer cross folders; the GPU host gets the same
   structure in its own checkout). (b) `tools/results/` (same, but inside the unit). (c) Flat
   `results/`.
   Move the existing result files into the new structure (recommended, so the m1/m2 compare commands
   still find them), or leave the old folders as an archive?
   **DECISION**: **a** and move existing result files.

9. **Environment files.** The same values appear in several `.env` files, and the complaint API
   reads LangSmith settings from `../m4/.env`.
   (a) **One `.env` per unit, each holding only what that unit reads; LangSmith settings move to
   `app/.env`** (the app is what exports traces), and `TRACING_ENV_FILE` goes away (recommended).
   Values shared by `app` and `tools` (`INFERENCE_MODEL`, `INFERENCE_EXTRA_BODY`,
   `TACO_ADMIN_TOKEN`) are still duplicated, with a comment pointing to the source.
   (b) One root `.env` used by every project via `--env-file ../.env` on each command. No
   duplication, but every runbook command gets longer and podman-compose 1.0.6's `--env-file`
   support must be checked.
   (c) (a), plus `tools/compose.yaml` reads `../app/.env` for the inference values through `env_file:`
   so they are set in one place. Keeps commands short but brings back one cross-folder link.
   **DECISION**: **a**

10. **Module 2 and the shared `app/compose.yaml`.** After the merge, module 2 learners use a compose
    file that also defines `complaint-api` (module 3). Runbooks always name services
    (`up -d case-api`), so module 2 never starts it. Acceptable, with a comment block in the compose
    file saying which module introduces each service? The alternative is a module 2-only override
    file, which reintroduces per-module compose files.
    **DECISION**: That is acceptable.

11. **Final-state repository vs per-module snapshots.** The repository already holds the final code,
    with later features off by default (m4 decision 1). Keep that model and add git tags at the end
    of each module (`m1-complete` ... `m6-complete`) so learners can check out the state a recording
    used? Tags for already-finished modules would point at the post-migration commit, so they mark
    the layout, not the exact recording state.
    **DECISION**: Students will be working with the final-state only. No need for additional tagging.

12. **GPU host project.** Merge `taco-m1` and `taco-model-exporters` into one project `taco-model`
    in `model-host/compose.yaml` (recommended: matches the outline's deployment unit; services are
    always started by name, so exporters still start and stop independently of vLLM). Or keep the
    exporters as a second project to keep "monitoring never touches vLLM" visible in the file layout.
    **DECISION**: Merge into one project.

13. **Model-host-local tool runs.** m1 runbooks run the `tools` container on the GPU host attached to
    the inference network (`BASE_URL=http://gateway:8080/v1`). With tools in their own project, it
    would join `taco-model-inference` as an external network on the GPU host and use the published
    gateway URL on the desktop. That needs either two network variants in `tools/compose.yaml` or a
    small override. Alternatively, on the GPU host, target `http://host.containers.internal:8080/v1`
    with no shared network. I recommend `host.containers.internal` if it works on the GPU host's
    Podman version; I will give you a command to check it.
    **DECISION**: Go with host.containers.internal and we'll test it with podman. I can always upgrade to a newer podman if necessary.

14. **Commits.** Do the migration as a sequence of commits on a branch (one per phase in section 4,
    using `git mv` so history follows the files), and you review and merge? Or should I only make
    the working-tree changes and leave committing to you?
    **DECISION**: Make working tree changes and I'll commit them. This repository is not live yet.

15. **Timing of the GPU host step.** Phase 2 recreates vLLM and the gateway under the new project
    (one model reload, about 1-3 minutes). Any constraint on when that can happen?
    **DECISION**: No.

---

## 4. Migration phases

Each phase leaves the repository working. Volumes and networks on the desktop keep their names, so
no data is migrated; only containers and images are recreated.

| Phase | Work | Verification |
| --- | --- | --- |
| 0. Baseline | Record current state: `podman ps`, `podman volume ls`, `podman network ls` on both hosts; run `shared/tests`, `m2/tests`, `m3/tests`; note the last good e2e and baseline results. Tag the pre-migration commit (`pre-layout-migration`). | all tests pass |
| 1. Python packages | Rename packages (Q5) in place first (`complaint_api`, `teaching_agent`, `taco_tools`), update imports and entry points. No folder moves yet, so failures are import errors only. | all tests pass; m3 image builds |
| 2. `model-host/` | Move m1 serving files and m4 exporters; one `taco-model` project; explicit network name. Update `env.sh` (`COMPOSE_FILE`, project name). On the GPU host: stop and remove the old projects' containers and the `taco-m1_inference` network, start `gateway`, `serve.sh qwen`, exporters. | `/health`, smoke test from the desktop, Prometheus `vllm`, `node`, `gpu` targets up |
| 3. `app/` | Merge m2 and m3 compose files and images; move `case_api` (Q6), clients, teaching script, tests; new `.env.example` with the LangSmith block; root `secrets/` (Q7). Recreate `case-api` and `complaint-api` from `app/`. | `tests` service passes; `cli submit A01` reaches review; existing pending cases and approvals survive (same volumes); LangSmith trace appears; `basic-agent --check` works |
| 4. `observability/` | Move Prometheus and Grafana config; split `m4/.env`. Recreate both services. | targets up; history before the migration still visible; dashboards unchanged |
| 5. `tools/` | One tools project and image (Q3, Q4); move m1 tools, m2/m3 harnesses, Locust, baseline report; root `results/` (Q8) and move existing results. | smoke, `screen` (1 scenario), `agent-eval` (1 scenario), `failure-tests --only F01`, `e2e-eval` (subset), `load --mode app --shape steady` (60 s), `baseline` all run and write to `results/` |
| 6. Documentation | Unit READMEs; module READMEs reduced to overview and links; every `demo-*.md` updated (`cd app`, `cd observability`, `cd tools`, new service names); root README structure table; ARCHITECTURE.md module sections; `.gitignore` and `.dockerignore`; `m4/implementation_status.md` and `m5/planning.md` layout sections; outline command flow (Q2). | every runbook command for modules 1-4 executed once in the new layout |
| 7. Cleanup | Remove old images (`taco-m1-tools`, `taco-m2-agent`, `taco-m3-app`, `taco-m4-load`) and empty module subfolders; delete old `.env` files only after their values are in the new ones. | `git status` shows only intended changes; no references to old paths (`git grep` for `m[1-4]/(app|eval|load|tools|...)`, `../m`) |

### What changes in the runbooks (examples)

| Before | After |
| --- | --- |
| `cd m3 && podman-compose up -d --force-recreate complaint-api` | `cd app && podman-compose up -d --force-recreate complaint-api` |
| `cd m3 && podman-compose run --rm cli submit A01` | `cd app && podman-compose run --rm cli submit A01` |
| `cd m3 && podman-compose run --rm e2e-eval --label x` | `cd tools && podman-compose run --rm e2e-eval --label x` |
| `cd m4 && podman-compose run --rm load --mode app --shape burst` | `cd tools && podman-compose run --rm load --mode app --shape burst` |
| `cd m4 && podman-compose up -d prometheus grafana` | `cd observability && podman-compose up -d prometheus grafana` |
| `source m1/env.sh && ./m1/serve.sh qwen` | `source model-host/env.sh && ./model-host/serve.sh qwen` |
| `podman compose run --rm tools tools/smoke_test.py ...` (GPU host) | `tools run --rm smoke ...` (the `tools` function from `model-host/env.sh`) |
| `podman compose -f m4/model-host/compose.yaml up -d node-exporter dcgm-exporter` | `podman compose up -d node-exporter dcgm-exporter` (with `model-host/env.sh` sourced) |

---

## 5. Risks to check before or during the move

- **podman-compose 1.0.6 path and `.env` resolution**: relative `build.context`, bind mounts,
  `secrets.file`, and `env_file` resolve from the compose file's folder; confirm `../secrets/...`
  works for `secrets:` with this version before committing to Q7 (a).
- **External networks from a third project**: `tools` joins `taco-app-private` (owned by `taco-app`)
  and `taco-telemetry`; already proven for `load` in module 4, so low risk.
- **Same subnet on the GPU host**: the new `taco-model-inference` network reuses 10.89.7.0/24 so
  nginx's fixed upstream IP stays valid; the old network must be removed first or creation fails.
- **`container_name` collisions**: `vllm`, `gateway`, and exporter containers have fixed names; the
  old project's containers must be removed before the new project starts them.
- **Recorded results referenced by name** in runbooks and implementation status files (for example
  `baseline-app-baseline-20261012-184024.md`): moving them changes links; phase 6 updates them.
- **Healthchecks and entry points** that call scripts by path (`python eval/failure_tests.py`,
  `uvicorn app.main:app`) all change in phase 1; the `tests` service catches import errors but not
  compose entry points, so phase 5 runs each one-shot once.
- **APP_VERSION and `taco_build_info`**: the image name changes; Grafana panels filter on labels,
  not image names, so no dashboard change is expected.
- **Learners following old recordings**: nothing has been recorded yet, so no old-to-new mapping is
  kept in the module READMEs.

---

## 6. As built (2026-10-14)

### Done

| Phase | Result |
| --- | --- |
| 0. Baseline | `shared/tests` 114, `m2/tests` 9, `m3/tests` 15 passed before any move. No tag was created (you commit). |
| 1-5. Moves | Done with `git mv`, so history follows the files. Layout as in section 2, with the deviations below. |
| 6. Documentation | Unit READMEs (`model-host/`, `app/`, `observability/`, `tools/`), module READMEs reduced to "components used", every runbook in `m1/`-`m4/`, root README, ARCHITECTURE.md (new "Deployment units" section and module diagrams), `shared/README.md`, `secrets/README.md`, `.gitignore`, Outline.md deployment-unit table and command flow, `m5/planning.md` decisions 1-2 and layout. |
| 7. Cleanup (partial) | Empty module subfolders removed. Old images, old `.env` files, and old secret files are left for you (below). |

Tests after the move: 119 + 16 + 3 = 138 passed, the same 138 tests as before, regrouped (locally and
in the `tools` container with `podman-compose run --rm tests`).

Live checks on the desktop (WSL, Podman 4.9.3, podman-compose 1.0.6) against the running Qwen3-14B:

| Check | Result |
| --- | --- |
| `app/`: build, recreate `case-api` and `complaint-api` | both healthy on the new `localhost/taco-app` image; existing volumes and cases kept; `tracing: true` read from `app/.env` |
| `../secrets/inference_api_key` as a compose secret | works with podman-compose 1.0.6 |
| `cli reset` + `cli submit A01` | `pending_review`, partial refund 8.50, 4 tool calls |
| `basic-agent --check` | endpoint, database, and case API OK |
| `tools/`: `smoke --skip-structured` | deployment record written to `results/serving/` |
| `failure-tests --only F01` | PASS, written to `results/failure/` |
| `e2e-eval --only A01` | passed 1.00, written to `results/e2e/` (see the note on a stalled model call below) |
| `agent-eval --only A01` | written to `results/agent/` |
| `observability/`: recreate `prometheus`, `grafana` | all five targets up; samples from 24 h earlier still queryable |
| `load --mode app --shape steady --duration 60` + `baseline` | 10 proposals reached review, 9 approved and recorded, 0 failures; baseline `.json`/`.md` written to `results/baseline/` |

The verification result files were deleted afterwards.

### Deviations from the plan

1. **The teaching loop and the API client are in `shared/`**, as `taco_shared.teaching_agent` and
   `taco_shared.api_client`, not in `app/`. Both are used by `app/` (teaching script, CLI) and by
   `tools/` (agent-eval, harnesses); `shared/` avoids one unit importing another's code.
2. **The case API runs from the app image** (`uvicorn case_api.main:app`), so `localhost/taco-case-api`
   and its Containerfile are gone. Health checks are defined per service in `app/compose.yaml`.
3. **Tests**: one `tests` service in `tools/` runs `shared/tests`, `app/tests`, and `tools/tests`
   against the mounted source. The module 2 tests were split: loop tests to
   `shared/tests/test_teaching_agent.py`, the script test to `app/tests/test_basic_agent.py`, the eval
   test to `tools/tests/test_agent_eval.py`. The case API tests moved from `shared/tests` to
   `app/tests/test_case_api.py`. The harness tests drive the real complaint API, so
   `tools/tests/conftest.py` re-exports the app test fixtures.
4. **Lock files**: `app.txt` (agent + service) and `tools.txt` (all extras) replace `core.txt`,
   `agent.txt`, `service.txt`, and `load.txt`; both are constrained to `dev.txt` and pin identical
   versions. `dev.txt` stays for local development.
5. **Results subfolders**: `serving/`, `screening/`, `sweep/` (m1 load sweeps), `agent/`, `e2e/`,
   `failure/`, `load/`, `baseline/`. Each tools service sets `RESULTS_DIR` to its subfolder. The e2e
   and failure harnesses now honor `RESULTS_DIR` (they wrote to a fixed `results/`), and the baseline
   report reads load runs from `LOAD_RESULTS_DIR`.
6. **GPU host `tools` function** in `model-host/env.sh`: `env.sh` exports `COMPOSE_FILE` and
   `COMPOSE_PROJECT_NAME` for the model host project, which would otherwise capture tools commands.
   `tools build` / `tools run --rm smoke ...` work from any directory. `env.sh` also exports
   `RESULTS_ROOT`; `RESULTS_DIR` is now `results/serving`.
7. **LangSmith settings** are passed to `complaint-api` by `${VAR}` interpolation from `app/.env`;
   `tracing-off.env` and `TRACING_ENV_FILE` are gone, and quoting is no longer an issue.
8. **Serving tools discover the model** when `--model` is not given (as module 1 did); `tools/.env`'s
   `INFERENCE_MODEL` applies only to `agent-eval` and direct `load`.
9. **`python` service** in `tools/` replaces `podman compose run --rm tools -c '...'` (Demo 1.2 step 1).

### Local files I created or moved (not in git)

- `app/.env`, `observability/.env`, `tools/.env` built from the templates with the values from
  `m3/.env` and `m4/.env` (LangSmith settings included).
- `secrets/inference_api_key` and `secrets/grafana_admin_password` copied from `m3/secrets/` and
  `m4/secrets/`.
- Desktop result files moved from `m1/`-`m4/results/` into `results/<kind>/`.

One file was lost: while deleting my verification results with a `*migration-check*` pattern, I also
deleted `results/screening/screening-llama-3.1-8b-migration-check-20261001-103916.json`, a desktop
result from 2026-10-01 that happened to use the same label. It was untracked, so git cannot restore
it. If the GPU host has the same file in its `m1/results/`, that copy is intact.

### Remaining steps for you

**GPU host** (after you commit and pull there):

```bash
cd ~/Deploying-and-Operating-Small-Language-Model-Systems
# 1. Remove the old projects' containers (fixed names) and network; the hf-cache volume stays.
podman rm -f vllm gateway node-exporter dcgm-exporter nvidia-smi-exporter 2>/dev/null
podman network rm taco-m1_inference
# 2. Move the old results into the new layout (git leaves untracked files in m1/results).
mkdir -p results/serving results/screening results/sweep
mv m1/results/deployment-record-* m1/results/last-serving-args.txt m1/results/vram-* results/serving/ 2>/dev/null
mv m1/results/screening-* results/screening/ 2>/dev/null
mv m1/results/load-* results/sweep/ 2>/dev/null
# 3. Start the model host project
source model-host/env.sh
podman compose up -d gateway
./model-host/serve.sh qwen            # with the module 1.3 tuned settings you use, e.g. --kv-cache-dtype fp8
podman compose up -d node-exporter dcgm-exporter
# 4. Tools on this host: build, then check that host.containers.internal reaches the gateway
tools build
tools run --rm python -c 'import os, urllib.request as u; b = os.environ["BASE_URL"]; print(b, u.urlopen(b + "/models", timeout=5).status)'
tools run --rm smoke --skip-structured --extra-body "$QWEN_EXTRA_BODY"
```

Verified (2026-10-14): from a tools container on the GPU host, both
`http://host.containers.internal:8080/v1` (the default) and `http://$MODEL_HOST:8080/v1` return 200,
so `tools/.env` is optional there. Also confirm that `tools run` does not
complain about the missing `secrets/inference_api_key` or the desktop-only external networks
(`taco-app-private`, `taco-telemetry`, `taco-app-data`); if it does, create an empty
`secrets/inference_api_key` on the GPU host and tell me which check failed.

**Desktop** (optional cleanup, once you are happy with the new layout):

```bash
podman rmi localhost/taco-m2-agent localhost/taco-m3-app localhost/taco-m4-load localhost/taco-case-api localhost/taco-m1-tools
rm -r m2/.env m3/.env m4/.env m2/secrets m3/secrets m4/secrets   # old local config; values are in the new files
```

### Observed during verification (not caused by the migration)

The first `e2e-eval --only A01` run ended `escalated (timeout)` because a single model call took 109 s
without an error; a rerun passed in 7.7 s. The app image pins the same package versions as before, so
the migration is an unlikely cause. It resembles the hang in m4 deviation 1 (fixed there), and the
cause here is unknown. It is worth a look before recording, since an e2e or load run can hit it.
