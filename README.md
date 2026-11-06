# evalcore — headless evaluation platform (prototype)

A configuration-driven, API-only evaluation engine for Canadian small and
medium businesses. One engine scores very different things — job candidates,
suppliers, AI agents — from JSON templates that the customer's own team owns.

- **Zero-deploy changes.** Raising supplier quality weight from 60% to 70% is a
  `PATCH` to the template, not a code release. Every save is a new immutable
  version; the next API call uses it; rollback is one call.
- **Developer-owned scoring.** Each template carries its own Python scoring
  script. Developers test drafts in a dry-run sandbox without touching
  production traffic.
- **Headless.** No UI of its own. Microsoft 365 / Teams, Google Workspace,
  Slack, BambooHR, QuickBooks Online or an in-house portal call the REST API;
  end users never see this platform.
- **Canadian privacy built in.** Templates that evaluate people must declare
  purpose, retention, consent and an automated-decision notice (PIPEDA,
  Quebec Law 25, Ontario's AI-in-hiring disclosure). Results about people go
  to human review; access, erasure and retention purge are API calls.
- **English / French.** Any template text can be `{"en-CA": ..., "fr-CA": ...}`;
  responses follow `Accept-Language`.
- **Lead-gen diagnostics.** Question-based risk calculators for MSPs,
  accounting firms and agencies: skip logic, weighted risk categories, risk
  tags and sales hooks; an instant public score for the website and a silent,
  asynchronous push of the full lead to HubSpot or a signed webhook
  (Zapier / Make). Canadian data residency and CASL consent are enforced.
  See [docs/LEADGEN.md](docs/LEADGEN.md) and run `python examples/demo_leadgen.py`.

> Status: prototype. Standard library only (Python 3.11+), SQLite storage,
> process-level sandbox. See [docs/ROADMAP.md](docs/ROADMAP.md) for the path
> to production — in particular, the sandbox is **not** a security boundary yet.
> The compliance checks are engineering guard-rails, not legal advice.

## Quick start

```bash
python -m evalcore serve                 # http://127.0.0.1:8700, seeds ./templates
```

```bash
curl -s -X POST localhost:8700/v1/evaluations -H "Content-Type: application/json" \
  -d "{\"templateId\":\"tmpl_vendor_scorecard\",\"payload\":$(cat examples/payloads/vendor_q3.json)}"
```

Change a weight with no redeploy (weights must still sum to 1.0):

```bash
curl -s -X PATCH localhost:8700/v1/templates/tmpl_vendor_scorecard/weights \
  -H "X-Actor: ops@example.ca" -d '{"DIM_QUALITY": 0.70, "DIM_PRICE": 0.05}'
```

Set `EVAL_API_KEY` before `serve` to require an `X-API-Key` header.

Debug a template locally without a server:

```bash
python -m evalcore validate templates/tmpl_universal_v1.json
python -m evalcore run templates/tmpl_universal_v1.json examples/payloads/ai_agent.json --trace
```

Run the tests:

```bash
python -m unittest discover -s tests
```

## How it works

```
payload ──► template check ──► payload check ──► sandbox script ──► output contract ──► result
            schema, weights,    channel schemas,  timeout, no        score 0-100,
            script compiles,    slider ranges,    imports, own       all dimensions,
            compliance rules    consent           process            band
```

Each stage writes to a trace that is returned by dry-run and stored with
every evaluation (`GET /v1/evaluations/{id}?trace=1`).

| Path | What |
|---|---|
| `evalcore/schema/` | JSON Schema for templates |
| `evalcore/validation.py` | structural + semantic template checks |
| `evalcore/compliance.py` | Canadian privacy rules |
| `evalcore/pipeline.py` | payload ingestion, output contract, `evaluate()` (script mode) |
| `evalcore/sandbox.py` | scoring-script execution |
| `evalcore/assessment.py` | question-based scoring: skip logic, deductions, risk, tags, hooks, the two payloads |
| `evalcore/conditions.py` | rule language shared by skip logic, tags, hooks and CRM mapping |
| `evalcore/residency.py` | data-residency policy for storage and outbound CRM |
| `evalcore/crm/` | CRM mapping, HubSpot and webhook adapters, async outbox dispatcher |
| `evalcore/store.py` | migrations; templates, evaluations, submissions, outbox, audit log (SQLite) |
| `evalcore/api.py` | REST API (admin `/v1`, public `/public/v1`) |
| `templates/` | example templates (seeded on `serve`) |
| `examples/payloads/` | example payloads |
| `docs/` | [API reference](docs/API.md), [template guide](docs/TEMPLATES.md), [roadmap](docs/ROADMAP.md) |
