# Intake — enquiries from the website and WhatsApp

Release 343 opened one door for every enquiry that does not start at the desk;
release 344 moves the RM's Approve / Reject click into PRISM. The website posts
each enquiry to PRISM the moment it is submitted, PRISM answers with an Approve
link and a Reject link, the website puts them in the e-mail it sends the RM, and
the RM's click lands on PRISM directly — no website server in between.

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
r = requests.post(URL, data=raw, headers={"Content-Type": "application/json",
                  "X-Intake-Timestamp": ts, "X-Intake-Signature": f"sha256={sig}"})
links = r.json()["links"]          # one {recipient, approve_url, reject_url} per approver
```

## The body

The website's enquiry JSON as agreed, with `status` saying which way in:

| field | required | notes |
|---|---|---|
| `enquiry_no` | yes | the idempotency key; a redelivery answers with the stored outcome and writes nothing (a redelivery of a still-waiting enquiry re-issues its links) |
| `channel` | no | `website` (default) or `whatsapp` |
| `status` | yes | `submitted` — PRISM hosts the approval (below); `approved` / `rejected` — the sender already decided, the rule runs at once |
| `approvers` | for submitted | the e-mail addresses the website will send the Approve / Reject mail to; one pair of links per address, so the click says who approved. Empty = one unnamed pair; the BD Head then owns the lead |
| `intent` | yes | `capital` or `assets` |
| `approved_by`, `approved_at` | for approved | the RM's e-mail; resolved against the Employees roster |
| `contact.name`, `contact.mobile`, `contact.email` | name yes | mobile is normalised to +91 |
| `company.name` | yes | the lead's company; matched canonically against the client master |
| `company.cin`, `company.address`, `company.city`, `company.state` | no | state and city are parsed from the address when not given ("…, Hyderabad, Telangana 500081") |
| `capital.*`, `assets.*`, `business.*`, `message` | as sent | kept whole in the lead's notes; `capital.sector` maps to the register's Sector list |

Unknown fields are stored with the enquiry and ignored.

## PRISM-hosted approval (`status: submitted`)

1. The website posts the enquiry at submission with `approvers`. PRISM parks it
   (status `submitted`, outcome `pending`) and answers `201`:

   ```json
   {"enquiry_no": "EV483920", "status": "submitted", "outcome": "pending",
    "links": [{"recipient": "mukesh@evamfinance.com",
               "approve_url": "https://prism-evamfinance.com/v1/intake/enquiries/<token>/approve",
               "reject_url":  "https://prism-evamfinance.com/v1/intake/enquiries/<token>/reject",
               "expires_at": "2026-10-13T09:36:00+00:00"}],
    "approve_url": "…", "reject_url": "…", "replayed": false}
   ```

   `approve_url` / `reject_url` at the top level are the first pair, for the
   one-approver case. Each recipient must get **their own** pair in their own
   e-mail; the token is what tells PRISM who approved.
2. The website sends the RM the e-mail with the two links as buttons. Nothing
   else is needed from the website: no approve handler, no second post.
3. The RM taps Approve. The link opens a PRISM page showing the enquiry
   (company, contact, ask, address) and one button. **Opening the link changes
   nothing** — Gmail and mail scanners open links to preview them — so the
   decision is the button press, a POST. Pressing it runs the rule below and
   shows the result ("Lead LD-404 created … assigned to Mukesh Rao").
4. Reject opens the same page with an optional reason box; the reason is kept
   with the enquiry. Nothing is created.
5. A link is good for seven days (`REGISTER_INTAKE_TOKEN_TTL_DAYS`), bound to
   that enquiry, that action and that recipient, and spent by the first
   decision: every later click — same link, the other link, another approver's
   link — shows "Already approved by … on …" / "Already rejected" and creates
   nothing. An expired link says so (410) and the enquiry stays waiting.
6. `approved_by` is the address the link was issued to, never something the
   click can claim. An approver who is not on the Employees roster hands the
   lead to the BD Head, flagged in the notes.

Only the SHA-256 of each token is stored (`lead_enquiry_tokens`); a copy of the
table cannot be turned into a link. The links carry the public origin from
`INTAKE_PUBLIC_BASE_URL` in `.env` (default: the request's host).

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
temperature Warm; sector and lens from the enquiry; the whole enquiry in notes
("… · approved by mukesh@evamfinance.com"); a next action for tomorrow; last
touch = the approval date. The RM gets an inbox notification (and e-mail through
the notifier) either way.

## The answer to a decided delivery

```json
{"enquiry_no": "EV483920", "status": "approved", "outcome": "lead_created",
 "lead_id": "…", "lead_no": "LD-404", "deal_id": null, "interaction_id": null,
 "rm": "Mukesh", "note": "New lead and client master row.", "replayed": false}
```

`201` on every accepted delivery, including a replay (`replayed: true`).
`403` unsigned, stale or wrongly signed; `422` an enquiry without a company
name or with an unknown status; `503` intake not configured.

## Where to look afterwards

* Leads grid: source Inbound, the enquiry number in Source detail.
* Activity log: one `intake.enquiry` row per delivery (status `submitted`) and
  one per decision (`approved` / `rejected`, actor = the approver).
* Table `lead_enquiries`: every enquiry as received, who decided it and when,
  the outcome and the lead or deal it landed on; `lead_enquiry_tokens`: the
  links issued, hashed, with their expiry and use.
