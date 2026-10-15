# Demo 5.1: Monitor output quality

Goal: sample completed responses, score them with deterministic checks and (for synthetic cases) a
label, flag degraded ones for a human rubric review, and follow a schema-valid but wrong proposal to its
request, trace, model version, and policy version.

Prerequisites: the module 5 setup in [README.md](README.md) (sampling on, `taco-slm` alias, images
built). Commands run from `tools/` unless noted. All data is synthetic.

Sample output below comes from the test run on 2026-10-14 (Qwen3-14B). S09 and S15 failed the same
way in every run so far; S12 fails in some runs and passes in others.

---

## Clip 1: continuous quality checks (slides)

- **Three questions, three tools.** Is it up and fast (metrics, module 4)? Is the answer right (checks and
  labels)? Is it a good answer for the customer (the rubric, a person)?
- **Validation is not quality.** Every proposal that reaches review already passed `validate_proposal`:
  the schema, items on the order, amount within the total, the approval threshold, already compensated.
  The quality checks recompute what the validator does not: the amount for the cited rule (R3 lateness
  tiers, R1/R2/R6 item prices, R4/R5 order total), whether a cited rule covers the category, an approval
  flag that is required *or unnecessary*, the filing window, duplicate actions, and the policy version.
- **What the checks cannot see.** A proposal that is consistent but answers the wrong question (a food
  quality complaint treated as a wrong item) passes every check. On synthetic data a label catches it; in
  production only a person does.
- **Sampling.** `QUALITY_SAMPLE_RATE` picks cases deterministically by case id (a replay samples the same
  cases). Escalations, clarifications, rejections, and corrected proposals are always sampled.
- **Thresholds** ([quality-gates.yaml](../tools/quality-gates.yaml)): a sample is flagged on any failed
  check, a failed label, or a rubric score of 2 or less. A model is degraded when, over its last 20 scored
  samples (at least 5), checks pass under 90%, the label pass rate falls more than 10 points under the
  baseline (0.88), or escalations exceed twice the baseline rate.

## Clip 2: implementing quality monitoring

### 1. The pipeline in the code

- `shared/taco_shared/quality/sampling.py`: `sample_reason()` and `build_record()`, called by
  `ComplaintService._sample()` (`app/complaint_api/service.py`) after every workflow run. One sample per
  case thread, updated at review and at the outcome; never fails a request.
- `shared/taco_shared/quality/store.py`: `quality.db` on the `taco-app-quality` volume. The API writes
  samples; the `quality` tool writes scores; reviews come from both.
- `shared/taco_shared/quality/checks.py`: `run_checks()`, `grade_label()`, `successful()`.
- `tools/taco_tools/quality/quality.py`: the scorer and the review queue.

```bash
podman logs taco-app_complaint-api_1 2>&1 | grep complaint_api_ready | jq -c '{model, quality_sample_rate, tracing}'
```

### 2. Run labeled complaints through the application

```bash
podman-compose run --rm e2e-eval --only A01,S09,S12,S15 --label m5-quality
```

```
   S09 late_delivery_under_15                 FAIL recorded             tools=4 nudges=0 fixes=0 actions=1   8.5s
   S12 wrong_address_not_recovered            PASS recorded             tools=4 nudges=1 fixes=0 actions=1   9.7s
   S15 food_quality_incorrect_preparation     FAIL recorded             tools=4 nudges=1 fixes=0 actions=1   8.2s
   A01 showcase_two_missing_carnitas_tacos    PASS recorded             tools=4 nudges=0 fixes=0 actions=1   8.4s
```

The harness approves every proposal that reaches review, as a busy reviewer might: all four were
recorded. Each case's sample now holds the complaint, the evidence, the proposal, and the action.

### 3. Score the samples

```bash
podman-compose run --rm quality score --label m5-quality
```

```
scored 4 sample(s); 2 flagged
model      samples  checks pass  label pass  successful  escalated  flagged  degraded
qwen3-14b  4        0.75         0.50        2/4         0.00       2        no
```

Too few samples for a degradation verdict (minimum 5). The report is in
`results/quality/quality-m5-quality-<ts>.md`.

### 4. The review queue

```bash
podman-compose run --rm quality queue
```

```
sample               case        scenario  status    model      why flagged
CASE-10274:6847d4c7  CASE-10274  S15       recorded  qwen3-14b  label: policy_rule_ids must include ['R6'], got ['R2']; complaint_category should be one of ['food_quality']
CASE-10050:4731fbb4  CASE-10050  S09       recorded  qwen3-14b  amount_grounded: lateness tier is apology_only 0, proposal has store_credit 5.00; label: ...
```

### 5. A schema-valid, validator-accepted, wrong proposal (S09)

```bash
podman-compose run --rm quality show CASE-10050
```

Walk through it:

- The order was 12 minutes late. R3 gives `apology_only` under 15 minutes; the model proposed
  `store_credit 5.00` and cited R3. Valid JSON, valid schema, every validator rule satisfied, approved,
  recorded. `amount_grounded` recomputes the lateness tier from the order record in the evidence.
- **Correlate**: the `request` id matches the API's JSON log lines
  (`podman logs taco-app_complaint-api_1 2>&1 | grep <request id>`), the trace link opens the
  `submit` and `decision` runs in LangSmith, and the versions line names the model, the policy (2026.09),
  and the prompt hash in force when it was produced.

### 6. What only the label catches (S15)

```bash
podman-compose run --rm quality show CASE-10274
```

Every deterministic check passes: the model treated a food quality complaint as a wrong item and applied
R2 consistently. Only the label (synthetic data) flags it. In production nothing automatic would; that is
the evaluator-limitations point, and the reason for a labeled reference set before every release
(Demo 5.2) and a person on the queue.

### 7. Review in the browser

Open `http://127.0.0.1:8090/ui/quality?actor=qa-01` (`qa-01` is a quality reviewer: no submit or approve
rights). Open S09, read the checks and the customer message, score it with the [rubric](../tools/rubric.md),
choose a disposition (`model_quality`), and save. The sample leaves the queue. The CLI does the same:

```bash
podman-compose run --rm quality review CASE-10274 --tone 4 --helpfulness 3 --escalation 4 --safe no \
    --disposition model_quality --note "Treated incorrectly prepared churros as a wrong item"
```

### 8. Dashboards and alerts

Grafana, **Quality and cost**: checks and label pass over the last 20 scored samples by model, the review
queue, unscored samples, recorded resolutions per hour, and an estimated cost per successful resolution
(the hourly cost is a dashboard variable). After a full run (`e2e-eval --label m5-quality` without `--only`
and `quality score`), the window has enough samples for `QualityDegraded` to evaluate; with the baseline
numbers it stays inactive.

```bash
podman-compose run --rm quality report --label m5-quality     # compare with the baseline in quality-gates.yaml
```

### If a take does not reproduce

S09 and S15 have failed the same way in every run so far, but generation can vary. Rerun only that
scenario (`e2e-eval --only S09 --label m5-quality-retake`, then `quality score`), or show the recorded
sample from an earlier run with `quality show`.
