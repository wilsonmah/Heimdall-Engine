# Roadmap

The prototype proves the model: configuration-driven scoring, versioned
zero-deploy changes, a dry-run sandbox, a headless API and Canadian privacy
guard-rails. Below is the order to harden it, highest risk first.

## 1. Real sandbox isolation (blocker for any customer-written code)

Today scripts run in a child Python process with restricted builtins. That
stops mistakes, not attackers.

- Run each execution in a gVisor or Firecracker micro-VM (or AWS Lambda /
  Azure Functions in a Canadian region) with no network, read-only
  filesystem, CPU / memory / time limits enforced by the platform.
- Keep `sandbox.execute(tpl, data, trace)` as the seam.
- Consider a restricted expression language for simple weightings so most
  customers never need full Python.

## 2. Identity, tenancy, roles

- Multi-tenant: every row scoped by `tenant_id`; per-tenant API keys.
- SSO for admins via Microsoft Entra ID and Google Workspace (the two most
  common SMB identity providers in Canada); `X-Actor` comes from the token.
- Roles: template author, approver, reviewer (HR), auditor.

## 3. Data residency and storage

- PostgreSQL in a Canadian region (AWS ca-central-1 / ca-west-1, Azure
  Canada Central / East, GCP northamerica-northeast1/2). Replace `Store`.
- Encrypt payloads containing personal information at rest; field-level
  encryption for `compliance.personalDataFields`.
- Backups stay in Canada; document sub-processors.

## 4. Change management

- Draft → approve → publish workflow for template changes (two-person rule
  for anything that evaluates people).
- Canary / percentage rollout of a new version, with score-diff report
  against the previous version on recent payloads.
- Template promotion between environments from Git (CI calls `PUT`).

## 5. Integrations (headless delivery)

- Outbound webhooks on evaluation completed / review needed, with HMAC
  signatures and retries.
- Connectors: Microsoft Teams and Slack notifications; BambooHR, Workday,
  Dayforce, Humi / Collage for candidates; QuickBooks Online, Sage, NetSuite
  for vendors; Microsoft Forms / Google Forms for manual inputs.
- Async evaluations with a job queue for batch runs.
- OpenAPI spec generated from the routes; client SDKs.

## 6. Privacy and fairness

- Privacy impact assessment template per evaluation purpose (Quebec Law 25
  requires PIAs for certain projects).
- Explanation endpoint: which inputs drove a person's score.
- Adverse-impact monitoring across groups where the customer lawfully holds
  that data; human-rights review before scoring candidates automatically.
- Localized API error messages (French).
- Confirm requirements with Canadian privacy counsel before launch;
  the rules in `compliance.py` are a starting point, not legal advice.

## 7. Operations

- Structured JSON logs, request ids, metrics (latency, sandbox failures,
  pending-review backlog), alerting.
- Scheduled retention purge (`POST /v1/admin/purge-expired`) via cron.
- Rate limiting per tenant.
