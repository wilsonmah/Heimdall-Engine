# Writing templates

A template is one JSON file (schema: `evalcore/schema/evaluation-template.schema.json`).
Keep templates in Git; push them with `PUT /v1/templates/{id}` from CI.

## Sections

| Section | Purpose |
|---|---|
| `targetSchema` | what can be evaluated (`HUMAN_CANDIDATE`, `AI_AGENT`, `VENDOR_ENTITY`) and the primary-key field |
| `evaluationMatrix.dimensions` | scored dimensions; `code` like `DIM_X`, `weight` 0–1, weights sum to 1.0 |
| `dataIngestion.channels` | where inputs come from: `DYNAMIC_FORM` (fields), `WEBHOOK_PAYLOAD` / `DATA_PIPELINE` (`validationSchema`) |
| `calculationEngine` | the scoring script, entrypoint, timeout, memory limit |
| `outputConfiguration` | `thresholds.critical` / `thresholds.excellent` for bands |
| `compliance` | required when people can be evaluated — see below |

Human-facing text (`name`, dimension `name`/`description`, field `label`,
`compliance.purpose`, `disclosureNotice`) may be a string or `{"en": ..., "fr": ...}`.

## The scoring script

```python
def calculate_scores(ingested_data, matrix_config):
    # ingested_data: all channel fields merged into one dict
    # matrix_config: {"DIM_X": {"weight": 0.4, "name": ...}, ...}
    return {
        "compositeScore": 0-100,
        "breakdown": {"DIM_X": 0-100, ...}   # one entry per dimension
    }
```

Rules: no imports; only basic builtins (`min`, `max`, `round`, `sum`, `len`,
...); finish within `timeoutMs`. Read weights from `matrix_config` rather
than hard-coding them, so weight changes work without editing the script.

Test a draft before saving:

```bash
python -m evalcore run my_template.json my_payload.json --trace
```

## Compliance block (people only)

```json
"compliance": {
  "jurisdictions": ["CA-ON", "CA-QC"],
  "dataResidency": "CA",
  "purpose": {"en": "...", "fr": "..."},
  "retentionDays": 365,
  "consentRequired": true,
  "automatedDecision": {
    "humanReviewRequired": true,
    "disclosureNotice": {"en": "...", "fr": "..."}
  }
}
```

Enforced at save time:

- `purpose`, `retentionDays` (≤ 2555), `consentRequired: true`, and a
  `disclosureNotice` are required.
- With `CA-QC`: `humanReviewRequired: true`, and French `purpose` and
  `disclosureNotice`.
