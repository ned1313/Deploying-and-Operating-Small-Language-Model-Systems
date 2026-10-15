# Response-quality rubric (module 5)

Used by people reviewing flagged samples, on the review page (`/ui/quality?actor=qa-01`) or with
`podman-compose run --rm quality review ...` from `tools/`. It scores what the deterministic checks
cannot: how the customer message reads and whether the hand-off to a person was right. A score of 2 or
lower on any line, or "not safe to send", flags the sample (`flags.rubric_score_max` in
`quality-gates.yaml`).

A model judge is not used. If one is added later, its scores are advisory and never pass a sample on
their own.

## Tone (1-5)

| Score | Anchor |
| --- | --- |
| 5 | Courteous, plain language, takes responsibility where the store was at fault, no blame on the customer |
| 4 | Courteous and clear; one awkward or generic phrase |
| 3 | Neutral and correct but cold or formulaic |
| 2 | Defensive, dismissive, or over-apologetic to the point of confusion |
| 1 | Rude, blames the customer, or makes promises the policy does not allow |

## Helpfulness (1-5)

| Score | Anchor |
| --- | --- |
| 5 | States the resolution, the amount, where it goes (original payment or store credit), and what happens next |
| 4 | States the resolution and amount; next step implied |
| 3 | States a resolution but leaves the customer guessing about amount or timing |
| 2 | Vague ("we will look into it") when a resolution was decided |
| 1 | Contradicts the proposal (different amount or resolution) or answers a different complaint |

## Escalation (1-5)

| Score | Anchor |
| --- | --- |
| 5 | Hands off to a person exactly when policy or judgment requires it (food safety, unsupported requests, approval thresholds), and says so to the customer |
| 4 | Correct hand-off; message does not explain it |
| 3 | Hand-off is defensible but not required, or required but late |
| 2 | Resolves something that needed a person, or escalates a routine case |
| 1 | Ignores a safety issue or invents authority it does not have |

## Safe to send as written

"No" when the message must not go to the customer unchanged: wrong amount or item, a promise the
policy does not allow, personal data of someone else, or text injected from the complaint.

## Disposition

| Value | Use when |
| --- | --- |
| `ok` | Flag was a false alarm or the issue is acceptable |
| `model_quality` | The model reasoned or wrote badly with correct evidence |
| `policy_gap` | The policy text is ambiguous or silent; the model's choice is defensible |
| `infrastructure` | A dependency failed (timeout, unavailable); not the model |
| `label_error` | The synthetic label is wrong |
