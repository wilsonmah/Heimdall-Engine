# API reference (v1)

All `/v1` endpoints take and return JSON. Headers:

| Header | Purpose |
|---|---|
| `X-API-Key` | required when the server runs with `EVAL_API_KEY` |
| `X-Actor` | who is making a change; recorded in the audit log |
| `Accept-Language` | `en-CA` (default) or `fr-CA` (`en` / `fr` accepted); `?lang=` overrides |

Errors: `{"error": "...", "details": [...]}` with 400 (bad request), 401,
403 (origin), 404, 405, 409 (wrong state), 413, 422 (validation failed), 429 (rate limit).

## Public assessment API (website embeds)

No API key; send the publishable `X-Form-Key` from `POST /v1/forms`. CORS is
allowed for the form key's origins. Details in [LEADGEN.md](LEADGEN.md).

| Method | Path | |
|---|---|---|
| GET | `/public/v1/assessments/{id}` | intro, privacy / sharing / consent text, first question |
| POST | `/public/v1/assessments/{id}/next` | `{"answers": {...}}` → `{done, question, progress}` |
| POST | `/public/v1/assessments/{id}/submit` | `{"answers", "lead", "consent", "source"?, "website": ""}` → 201 public payload |

## Lead-gen admin

| Method | Path | |
|---|---|---|
| POST / GET | `/v1/forms` | create / list publishable form keys `{"templateId", "allowedOrigins"}` |
| POST / GET | `/v1/crm/destinations` | `{"name", "kind": "HUBSPOT"\|"WEBHOOK", "region", "config", "allowCrossBorder"?, "templateIds"?}` |
| POST | `/v1/crm/destinations/{id}/enable` · `/disable` | |
| GET | `/v1/crm/outbox` | `?status=PENDING\|DELIVERED\|FAILED\|BLOCKED&submissionId=` |
| POST | `/v1/crm/outbox/{id}/retry` | re-queue a FAILED delivery |
| GET | `/v1/submissions` | `?templateId=&email=` |
| GET | `/v1/submissions/{id}` | agency payload, public payload, delivery status |
| GET | `/v1/admin/residency` | residency policy and schema version |

`POST /v1/templates/{id}/dry-run` with `{"answers": {...}, "lead"?: {...}}`
on an assessment template returns both payloads plus a `crmPreview` of the
HubSpot properties, without storing or sending anything.

## Templates

| Method | Path | |
|---|---|---|
| GET | `/v1/templates` | active version of every template |
| GET | `/v1/templates/{id}` | active body; `?version=N` for a specific one |
| GET | `/v1/templates/{id}/versions` | history, newest first |
| PUT | `/v1/templates/{id}` | save a full template as a new version and activate it; `?note=` |
| PATCH | `/v1/templates/{id}/weights` | `{"DIM_X": 0.3, "DIM_Y": 0.7}` — change weights only |
| POST | `/v1/templates/{id}/rollback` | `{"version": N}` — reactivate an older version |
| POST | `/v1/templates/{id}/dry-run` | `{"payload": {...}, "draft": {...}?}` — run without storing anything |

Saves are validated first; an invalid template never becomes active. The
server bumps the patch version unless the body carries a higher semver.

## Evaluations

| Method | Path | |
|---|---|---|
| POST | `/v1/evaluations` | `{"templateId", "payload", "version"?}` → 201, or 422 with `failedStage` |
| GET | `/v1/evaluations` | `?templateId=&entityId=&status=&limit=` |
| GET | `/v1/evaluations/{id}` | `?trace=1` adds payload and trace |
| POST | `/v1/evaluations/{id}/review` | `{"reviewer", "decision": "CONFIRMED"\|"OVERRIDDEN", "finalScore"?, "reason"?}` |

Status: `SUCCEEDED` (machine/vendor targets — final immediately),
`PENDING_REVIEW` (people, when the template requires human review),
`REVIEWED`, `FAILED`.

`final` is the score to act on: `{"score", "band", "decidedBy": "system"|"human"}`,
or `null` while review is pending. Results about people include
`disclosureNotice` in the requested language and an `expiresAt` derived
from the template's `retentionDays`.

## Privacy operations

| Method | Path | |
|---|---|---|
| GET | `/v1/subjects/{entityId}` | everything held about one person (access request); an email also finds lead submissions |
| DELETE | `/v1/subjects/{entityId}` | erase their evaluations / submissions and queued CRM jobs; audit keeps only a hash |
| POST | `/v1/admin/purge-expired` | delete evaluations past retention (schedule daily) |
| GET | `/v1/audit` | template saves, rollbacks, reviews, erasures, purges |

## Payload shape

```json
{
  "targetType": "HUMAN_CANDIDATE",
  "entity_id": "cand-123",
  "consent": { "obtained": true, "obtainedAt": "2026-11-04T15:20:00-05:00" },
  "channels": {
    "ch_manual_ui": { "input_manager_score": 6 },
    "ch_automated_telemetry": { "execution_latency_ms": 2400 }
  }
}
```

The primary-key field name comes from the template's
`targetSchema.identifierMapping.primaryKey`. `consent` is required only for
people when the template sets `compliance.consentRequired`.
