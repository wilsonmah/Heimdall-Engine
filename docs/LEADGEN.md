# Lead-gen diagnostics

A question-based assessment (a "risk calculator") that a professional-services
firm (MSP, accounting firm, agency) embeds on its website. The prospect gets
an instant score; the firm's CRM silently gets the lead with an itemized
breakdown and talking points.

```
website ──GET /public/.../{id}──────────► intro, privacy notice, first question
        ──POST /public/.../{id}/next────► next applicable question (skip logic)
        ──POST /public/.../{id}/submit──► 201 public payload (~30 ms)
                                           │ same transaction
                                           ▼
                                      crm_outbox ──worker thread──► HubSpot / signed webhook
                                                     retries, backoff,      (Zapier, Make,
                                                     residency check        Salesforce Flow, Zoho)
```

Run the whole thing locally against a mock HubSpot:

```bash
python examples/demo_leadgen.py
```

It writes what each party receives to [`examples/outputs/`](../examples/outputs):
`*.1_public_response.json` (browser), `*.2_agency_payload.json` (webhook /
admin API) and `*.3_hubspot_received.json` (CRM properties and note).

## Template sections

See [`templates/tmpl_cyber_risk_smb.json`](../templates/tmpl_cyber_risk_smb.json).

| Section | What it holds |
|---|---|
| `targetSchema.allowedTypes` | `["LEAD"]` |
| `evaluationMatrix.dimensions` | risk categories with weights and per-level `feedback` (LOW / MEDIUM / HIGH) |
| `assessment.questions` | `BOOLEAN`, `SINGLE_CHOICE`, `MULTI_CHOICE` (points per option, `maxPoints` cap), `NUMBER`, `TEXT`; `dimension` makes a question scored; `showIf` is skip logic |
| `assessment.locales` | `["en-CA", "fr-CA"]` — every question, option, dimension name and summary must have each locale |
| `rules.riskLevels` | `{"high": 50, "medium": 75}`: score < 50 is HIGH, < 75 MEDIUM, else LOW |
| `rules.statusOverrides` | escalate overall risk for answer combinations (e.g. no backups = HIGH whatever the score) |
| `rules.tags` | operational tags: `enterprise_lead`, `critical_vulnerability`, `low_budget`, ... |
| `rules.salesHooks` | talking points with `priority` and `service`, in `rules.agencyLocale` |
| `crmMapping.hubspot` | contact / company property mapping, `note: true` for a talking-points note |
| `compliance` | `privacyNotice`, `sharingDisclosure`, `retentionDays`, optional `marketingConsent` and `crossBorderDisclosure` |

## Rules and skip logic

```json
{"field": "company_size", "op": "gte", "value": 10}
{"all": [{"field": "has_firewall", "op": "eq", "value": false},
         {"field": "mfa_coverage", "op": "eq", "value": "none"}]}
{"field": "score.DIM_BACKUP", "op": "lt", "value": 50}
```

Ops: `eq ne gt gte lt lte in nin contains exists missing truthy falsy`;
combine with `all`, `any`, `not`. Fields: a question id, `score.<DIM|overall>`,
`risk.<DIM|overall>`, `lead.<field>`, `tags`. A `showIf` may only read
earlier questions, so there can be no cycles. A skipped question never
triggers a rule (`ne` on a missing answer is false).

## Scoring

- Dimension score = points earned / points possible over the questions shown
  to this respondent × 100. Skipped questions do not count against them.
- Overall = weighted average of dimensions that had questions; weights are
  renormalized (an 8-person firm is not penalized for governance questions it
  never saw).
- Every lost point is itemized in the agency payload: question, answer label,
  points deducted, and impact on its dimension.

## The two payloads

| | Public (browser) | Agency (CRM / webhook) |
|---|---|---|
| Score, risk, dimension scores | rounded | exact, with points earned / possible |
| Feedback / summary | respondent's locale | — |
| Chart data | yes | — |
| Contact, consent record, source / UTM | — | yes |
| Raw answers, deductions, skipped questions | — | yes |
| Tags, sales hooks | **never** | yes, in the agency locale |

## CRM delivery

Configure destinations with the admin API (`POST /v1/crm/destinations`):

```json
{"name": "HubSpot", "kind": "HUBSPOT", "region": "us",
 "config": {"tokenEnv": "HUBSPOT_TOKEN"}, "allowCrossBorder": true, "templateIds": ["tmpl_cyber_risk_smb"]}

{"name": "Zapier", "kind": "WEBHOOK", "region": "ca",
 "config": {"url": "https://hooks.zapier.com/hooks/catch/...", "secretEnv": "ZAPIER_SECRET"}}
```

- Secrets are never stored: config names an environment variable.
- HubSpot: contact upserted by email, company matched by domain and
  associated, plus an optional note with the talking points. Create the
  custom properties (`cyber_risk_score`, `cyber_risk_tags` as a multi-checkbox,
  ...) in HubSpot first.
- Webhooks are signed: `X-Evalcore-Signature: v1=HMAC-SHA256(secret, "{timestamp}.{body}")`.
  `X-Evalcore-Delivery` stays the same across retries; use it to dedupe.
- Status per delivery: `PENDING → SENDING → DELIVERED`, or `FAILED` (retry with
  `POST /v1/crm/outbox/{id}/retry`), or `BLOCKED` (residency).
- Salesforce and Zoho: use the webhook with Salesforce Flow / Zoho Flow for
  now; native adapters are on the roadmap.

## Canadian compliance

- **Residency.** `EVAL_DATA_RESIDENCY=CA` (default) refuses to start unless
  `EVAL_DATA_REGION` is a Canadian region (`ca-central-1`, `ca-west-1`,
  `canadacentral`, `canadaeast`, `northamerica-northeast1/2`). Outbound leads
  only go to destinations with a Canadian `region`. A destination outside
  Canada needs **both** `allowCrossBorder: true` and a
  `compliance.crossBorderDisclosure` in the template that tells the person.
  HubSpot accounts are usually hosted in the US or EU — check yours.
- **PIPEDA.** The privacy notice and sharing disclosure are shown before
  submit, and acknowledging them is required. Retention is enforced by
  `POST /v1/admin/purge-expired`. Access and erasure work by email:
  `GET` / `DELETE /v1/subjects/{email}`. Erasure here does not reach the CRM;
  delete the contact there too.
- **CASL.** Marketing consent is a separate, unchecked opt-in. The record
  keeps the exact wording shown, the sender name, the time and the method,
  and is mapped to `cyber_casl_consent` / `cyber_casl_consent_date` in HubSpot.
- **Silent sync is not secret.** It means the visitor doesn't wait for it.
  The sharing disclosure is what makes the transfer to the sales team lawful.

These are engineering guard-rails, not legal advice.

## Website embed

1. `POST /v1/forms {"templateId": "...", "allowedOrigins": ["https://www.example.ca"]}`
   returns a publishable key (`pk_...`). It is safe to ship in front-end code: it
   only allows the public routes for that one assessment, from those origins.
2. The front end sends `X-Form-Key` and `Accept-Language` on every call,
   includes an empty hidden `website` field (honeypot), and shows the
   `privacyNotice`, `sharingDisclosure` and `marketingConsent` text from the
   intro response.
3. Rate limit: 10 submits / 10 min / IP by default, held in memory (one
   instance only; use Redis when scaling out).
