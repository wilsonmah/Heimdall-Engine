"""HubSpot CRM v3 adapter.

Per lead:
  1. upsert the contact by email   POST /crm/v3/objects/contacts/batch/upsert
  2. find-or-create the company by domain, then update it
                                   POST /crm/v3/objects/companies/search, POST|PATCH /companies
  3. associate contact -> company  PUT  /crm/v4/objects/contacts/{id}/associations/default/companies/{id}
  4. add a note with the sales hooks for the rep (optional)
                                   POST /crm/v3/objects/notes

Progress is kept in `state` (persisted by the dispatcher after every attempt),
so a retry after a partial failure resumes instead of duplicating the note.

The custom contact properties named in the template's crmMapping (e.g.
cyber_risk_score) must already exist in the HubSpot account - create them once
under a property group such as "Risk Assessment". Destination config:
    {"tokenEnv": "HUBSPOT_TOKEN", "baseUrl": "https://api.hubapi.com"}
The token is a private-app token read from that environment variable; it is
never stored in the database.
"""
import html
import os

from . import PermanentError, mapping
from .http import request

NOTE_TO_CONTACT = 202  # HubSpot-defined association type id


class HubSpotAdapter:
    kind = "HUBSPOT"

    def send(self, dest, agency, tpl, state):
        cfg = dest["config"]
        token = os.environ.get(cfg.get("tokenEnv") or "", "")
        if not token:
            raise PermanentError(f"HubSpot token env var {cfg.get('tokenEnv')!r} is not set")
        base = cfg.get("baseUrl", "https://api.hubapi.com").rstrip("/")
        auth = {"Authorization": f"Bearer {token}"}
        spec = (tpl.get("crmMapping") or {}).get("hubspot")
        if not spec:
            raise PermanentError("template has no crmMapping.hubspot")
        ctx = mapping.context(agency)

        if not state.get("contactId"):
            props = mapping.build_properties(spec["contact"], ctx)
            _, out = request("POST", f"{base}/crm/v3/objects/contacts/batch/upsert",
                             {"inputs": [{"idProperty": "email", "id": props["email"], "properties": props}]}, auth)
            state["contactId"] = out["results"][0]["id"]

        if spec.get("company") and not state.get("companyId"):
            props = mapping.build_properties(spec["company"], ctx)
            if props.get("domain"):
                _, found = request("POST", f"{base}/crm/v3/objects/companies/search", {
                    "filterGroups": [{"filters": [{"propertyName": "domain", "operator": "EQ",
                                                   "value": props["domain"]}]}],
                    "properties": ["domain"], "limit": 1}, auth)
                if found.get("results"):
                    cid = found["results"][0]["id"]
                    request("PATCH", f"{base}/crm/v3/objects/companies/{cid}", {"properties": props}, auth)
                else:
                    _, created = request("POST", f"{base}/crm/v3/objects/companies", {"properties": props}, auth)
                    cid = created["id"]
                state["companyId"] = cid

        if state.get("companyId") and not state.get("associated"):
            request("PUT", f"{base}/crm/v4/objects/contacts/{state['contactId']}"
                           f"/associations/default/companies/{state['companyId']}", None, auth)
            state["associated"] = True

        if spec.get("note") and agency["salesHooks"] and not state.get("noteId"):
            _, note = request("POST", f"{base}/crm/v3/objects/notes", {
                "properties": {"hs_timestamp": agency["occurredAt"], "hs_note_body": note_body(agency)},
                "associations": [{"to": {"id": state["contactId"]},
                                  "types": [{"associationCategory": "HUBSPOT_DEFINED",
                                             "associationTypeId": NOTE_TO_CONTACT}]}]}, auth)
            state["noteId"] = note["id"]
        return {k: state[k] for k in ("contactId", "companyId", "noteId") if state.get(k)}


def note_body(agency):
    r = agency["result"]
    rows = "".join(f"<li><b>{html.escape(d['name'])}</b>: {d['score']} ({d['risk']})</li>" for d in r["dimensions"]
                   if d["score"] is not None)
    hooks = "".join(f"<li>{html.escape(h['text'])}</li>" for h in agency["salesHooks"])
    return (f"<p><b>{html.escape(agency['assessment']['name'])}</b> - score {r['score']} / 100, "
            f"risk {r['risk']}</p><ul>{rows}</ul><p><b>Talking points</b></p><ol>{hooks}</ol>")
