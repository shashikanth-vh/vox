# Intake — enquiries from the website and WhatsApp

Release 343. One door for every enquiry that does not start at the desk: the
website form (approved by an RM from the e-mail) and the WhatsApp bot post the
same JSON to PRISM, and the register decides what it becomes.

## The endpoint

```
POST https://prism-evamfinance.com/v1/intake/enquiries
Content-Type: application/json
X-Intake-Timestamp: <unix seconds, e.g. 1759737600>
X-Intake-Signature: sha256=<hex HMAC-SHA256(secret, timestamp + "." + raw body)>
```

* The secret is `INTAKE_WEBHOOK_SECRET` in `deploy/compose/.env`; share it with
  the website team only. Blank = the door is closed (503).
* The signature is over the exact bytes sent, prefixed by the timestamp and a
  dot. A timestamp more than five minutes off the register's clock is refused.
* No user token is needed; the gateway exempts this path and the register
  verifies the signature itself.

Python, for the sender:

```python
import hmac, hashlib, json, time, requests
raw = json.dumps(payload).encode("utf-8")
ts = str(int(time.time()))
sig = hmac.new(SECRET.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
requests.post(URL, data=raw, headers={"Content-Type": "application/json",
              "X-Intake-Timestamp": ts, "X-Intake-Signature": f"sha256={sig}"})
```

## The body

The website's approved-enquiry JSON as agreed, plus one optional field:

| field | required | notes |
|---|---|---|
| `enquiry_no` | yes | the idempotency key; a redelivery answers with the stored outcome and writes nothing |
| `channel` | no | `website` (default) or `whatsapp` |
| `status` | yes | `approved` or `rejected`; a rejection is stored and creates nothing |
| `intent` | yes | `capital` or `assets` |
| `approved_by`, `approved_at` | for approved | the RM's e-mail; resolved against the Employees roster |
| `contact.name`, `contact.mobile`, `contact.email` | name yes | mobile is normalised to +91 |
| `company.name` | yes for approved | the lead's company; matched canonically against the client master |
| `company.cin`, `company.address`, `company.city`, `company.state` | no | state and city are parsed from the address when not given ("…, Hyderabad, Telangana 500081") |
| `capital.*`, `assets.*`, `business.*`, `message` | as sent | kept whole in the lead's notes; `capital.sector` maps to the register's Sector list |

Unknown fields are stored with the enquiry and ignored.

## What the register does with an approved enquiry

In this order, so one company never gets two stories:

1. **The company already has a deal with a live line** → an interaction is
   logged on that deal ("Website enquiry EV483920: …"), the RM is notified, and
   no lead is created. Outcome `interaction_on_deal`.
2. **The company already has an Active lead** → an interaction on that lead,
   its next action set to "Call back on website enquiry …" for tomorrow.
   Outcome `interaction_on_lead`.
3. **The company is a client with no live work** → a new lead linked to the
   existing client. Outcome `lead_created`.
4. **The company is new** → a new lead, and the client master gains its
   Prospect row the way every desk-typed lead does. Outcome `lead_created`.
5. **The contact's mobile or e-mail is on another company's live lead** → the
   lead is still created, with `FLAG: same contact as lead LD-…` at the top of
   its notes.
6. **The approver is not on the Employees roster** → the BD Head owns the lead,
   flagged in the notes.

The lead carries: company, CIN, address, city, state; contact and phone; source
`Inbound`, source detail `Website EV483920`; RM = the approver's roster handle;
temperature Warm; sector and lens from the enquiry; the whole enquiry in notes;
a next action for tomorrow; last touch = the approval date. The RM gets an inbox
notification (and e-mail through the notifier) either way.

## The answer

```json
{"enquiry_no": "EV483920", "status": "approved", "outcome": "lead_created",
 "lead_id": "…", "lead_no": "LD-404", "deal_id": null, "interaction_id": null,
 "rm": "Mukesh", "note": "New lead and client master row.", "replayed": false}
```

`201` on every accepted delivery, including a replay (`replayed: true`).
`403` unsigned, stale or wrongly signed; `422` an approved enquiry without a
company name or with an unknown status; `503` intake not configured.

## Where to look afterwards

* Leads grid: source Inbound, the enquiry number in Source detail.
* Activity log: one `intake.enquiry` row per delivery with the outcome.
* Table `lead_enquiries`: every delivery as received, with the outcome and the
  lead or deal it landed on.
