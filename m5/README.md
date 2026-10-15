# Module 5: Evaluating Quality, Releasing Models, and Optimizing Cost

Runbooks for Demos 5.1-5.3. Module 5 samples completed workflow outcomes into an evaluation store,
scores them with deterministic checks (plus labels on the synthetic data and a human rubric), gates a
model release on measured results, rolls it back on the GPU host, and measures cost per successful
resolution.

| Demo | File | Outcome |
| --- | --- | --- |
| 5.1 | [demo-5.1-monitor-quality.md](demo-5.1-monitor-quality.md) | Sampled outputs scored and flagged; a schema-valid, validator-accepted proposal with the wrong amount caught by the checks, and a misclassification only the label catches; the review queue and the Quality and cost dashboard |
| 5.2 | [demo-5.2-release-and-rollback.md](demo-5.2-release-and-rollback.md) | Pinned release manifests; Llama-3.1-8B applied in a maintenance window, evaluated against the Qwen baseline, rejected by the release gates, and rolled back with one command |
| 5.3 | [demo-5.3-profile-and-optimize-cost.md](demo-5.3-profile-and-optimize-cost.md) | Cost per successful resolution for a baseline, an accepted optimization (more concurrency), and a rejected one (a smaller output budget) |

## Components used

| Folder | Used for |
| --- | --- |
| [app/](../app/README.md) | quality sampling in `complaint-api` (`QUALITY_SAMPLE_RATE`), the `taco-app-quality` volume, the review page at `/ui/quality`, the `taco_quality_*` metrics, actor `qa-01` |
| [tools/](../tools/README.md) | `quality` (score, report, queue, show, review), `release` (gate report), `cost`; plus `e2e-eval`, `failure-tests`, `load`, `baseline` labeled by release; [quality-gates.yaml](../tools/quality-gates.yaml), [rubric.md](../tools/rubric.md) |
| [model-host/](../model-host/README.md) | pinned image and revisions in `env.sh`, the stable `taco-slm` alias in `serve.sh`, [release.sh](../model-host/release.sh), [releases/](../model-host/releases/) |
| [observability/](../observability/README.md) | the Quality and cost dashboard and the `QualityDegraded` and `QualityReviewBacklog` alerts |
| [shared/](../shared/README.md) | `taco_shared/quality/`: sampling, the evaluation store, deterministic checks, labels, the successful-resolution rule |
| `results/` | `quality/`, `release/`, `cost/`, and the labeled `e2e/`, `failure/`, `load/`, `baseline/` runs |

## Data paths

| Path | Contents | Where |
| --- | --- | --- |
| Operational telemetry | ids, versions, timings, counts; no content | JSON logs, Prometheus, LangSmith (module 4) |
| Quality evaluation | the sampled complaint, evidence, proposal, validation, action; scores, flags, reviews | `taco-app-quality` volume (`quality.db`) |
| Audit | who requested, accessed, approved, executed | module 6 |

All three carry `request_id`, `case_id`, `thread_id`, and the deployment versions. The evaluation store
is separate from operational logs; module 6 adds access control and retention to it.

## Setup (once)

**GPU host**: `jq` (`sudo apt install jq`), the repository pulled, and the Llama weights in the model
cache (Demo 1.2 downloads them). Then serve the baseline under the stable alias:

```bash
source model-host/env.sh
chmod +x model-host/release.sh
./model-host/release.sh apply r1-qwen3-14b      # same model and settings as module 4, now also served as taco-slm
```

**Desktop**: in `app/.env` and `tools/.env` set `INFERENCE_MODEL=taco-slm`; in `app/.env` set
`QUALITY_SAMPLE_RATE=1.0`; set the cost assumptions in `tools/.env` (the defaults are placeholders).

```bash
cd app && APP_VERSION=$(git rev-parse --short HEAD) podman-compose build && podman-compose up -d --force-recreate complaint-api
cd ../tools && podman-compose build
cd ../observability && podman-compose up -d --force-recreate prometheus grafana    # new rules and dashboard
```

`curl -s http://127.0.0.1:8090/healthz` shows `"model": "qwen3-14b", "alias": "taco-slm"`.
