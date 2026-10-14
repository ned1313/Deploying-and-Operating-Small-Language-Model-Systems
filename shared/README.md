# shared

Code and data used by more than one deployment unit (`app/`, `tools/`, and the model host's tools).
The unit folders hold their compose files, Containerfiles, and entry points; module folders (`m1/`,
`m2/`, ...) hold only runbooks.

```
shared/
  pyproject.toml              package `taco_shared`; extras: agent (LangChain, LangGraph SQLite checkpointer), service (FastAPI, Prometheus client), dev (pytest), load (Locust)
  requirements/               exact pins: app.txt (app image: agent + service), tools.txt (tools image: all extras), dev.txt (local development)
  taco_shared/
    cli.py, errors.py, jsonutil.py, metrics.py, reporting.py   helpers used by the tools
    screening.py              module 1 prompts with order, prior-resolution, and policy facts inlined
    grading.py                grade(): schema, grounding, policy, and category checks against a labeled scenario
    policy.py                 versioned policy (JSON source, rendered text)
    policy_engine.py          reference outcomes from structured facts; labels the dataset
    proposal.py               proposal schemas v1/v2 and the evidence-based validator
    config.py, context.py     inference settings (+ secret file), the trusted agent context, actors and roles
    db.py, case_api_client.py read-only scoped SQLite queries; case API client (retry budget, idempotent record_action)
    agent/                    LangChain model factory, investigation tools, prompts, the proposal call, fault injection
    teaching_agent/           module 2 driver loop around create_agent, step rendering, scenario wiring
                              (used by app/examples/basic_agent.py and the agent-eval tool)
    workflow/                 module 3 LangGraph workflow: state, nodes, routes, budgets, approval interrupt
    api_client.py             HTTP client for the complaint API and case API (used by the CLI and the harnesses)
    testing.py                scripted chat model and in-process case API (loads app/case_api) for the test suites
    load.py                   module 4 workloads: the load-v1 case selection and direct-inference request bodies
    healthcheck.py            container healthcheck (`python -m taco_shared.healthcheck URL`)
    dataset/build.py          CSV -> generated data and scenario renderings
    dataset/seed.py           generated data -> SQLite (idempotent; --reset is explicit)
    data/
      source/                 taco_alley_customer_complaints.csv (1,000 rows, unmodified, with deliberate defects)
      menu.json, stores.json, actors.json, category_map.json
      policy/2026.09.json     rules R1-R14 (2026.09.txt is rendered from it)
      schemas/                proposal.v1.json (module 1), proposal.v2.json (module 2, generated)
      scenarios/curated.json  S01-S22 and A01-A02 on top of CSV rows
      scenarios/workflow.json module 3 variants of A01 (clarification, prompt injection, fault profiles), each with its own case
      generated/              committed build output; do not edit by hand (agent_scenarios.v2.json = agent-v2)
  tests/                      pytest; no GPU or network
```

## Rebuilding data

```bash
pip install -r shared/requirements/dev.txt && pip install -e shared --no-deps
python -m taco_shared.policy                 # re-render policy/2026.09.txt after editing the JSON
python -m taco_shared.proposal               # regenerate schemas/proposal.v2.json from v1
python -m taco_shared.dataset.build          # regenerate data/generated/
python -m taco_shared.dataset.build --check  # fail if committed data is stale
pytest -q shared/tests
```

The tools image runs every suite without a local Python: `cd tools && podman-compose run --rm tests`.

To change pinned versions, edit `pyproject.toml` and recompile the lock files (dev first; the others
are constrained to it):

```bash
uv pip compile shared/pyproject.toml --python-version 3.12 --python-platform x86_64-unknown-linux-gnu --extra agent --extra service --extra dev -o shared/requirements/dev.txt
uv pip compile shared/pyproject.toml --python-version 3.12 --python-platform x86_64-unknown-linux-gnu --extra agent --extra service -c shared/requirements/dev.txt -o shared/requirements/app.txt
uv pip compile shared/pyproject.toml --python-version 3.12 --python-platform x86_64-unknown-linux-gnu --extra agent --extra service --extra dev --extra load -c shared/requirements/dev.txt -o shared/requirements/tools.txt
```

The build drops CSV rows with impossible or future dates, empty fields, or malformed customer ids and
lists them in `generated/build_report.json`. Every synthesized value uses a per-row random generator,
so changing one row or one curated scenario does not shift any other row. Rows whose message does not
name a specific item for an item-level complaint are kept but labeled `ambiguous` (no expected outcome).

## Compatibility rule

Once a module is recorded, a change here must not break its runbook. Versioned artifacts (policy,
schemas, dataset) get a new version instead of an in-place edit, and every test suite (`shared/tests`,
`app/tests`, `tools/tests`) must pass before a change is merged.
