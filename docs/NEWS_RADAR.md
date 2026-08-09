# News Radar — search, digests and schedules

The Tools ▸ **India News Radar** screen: search the web for a company (or a founder, or
an executive), read what comes back with a severity the desk can act on, email it, and
have it emailed on a cadence without anyone asking.

It runs in **PULSE** (`services/pulse`), reached through the gateway at `/pulse/v1/news/*`.

## Why the search runs on the server

The browser cannot call Google News or GDELT: neither sends CORS headers, so the request
is blocked before it leaves the page. The UI used to route around that through a public
proxy (`allorigins.win`) — which put the names of the companies we are looking at through
a third party nobody vetted, and broke whenever that proxy did. PULSE fetches instead:
three sources, merged and de-duplicated, and the search terms never leave PRISM.

| Source | Date range | Notes |
|---|---|---|
| Google News RSS | yes (`after:` / `before:`) | the primary source |
| GDELT | yes | structured, but rate-limits hard — `PULSE_DISABLE_GDELT=1` skips it cleanly |
| Bing News RSS | no | stays up when the other two throttle |

Any one failing is normal and never fails the search. The same story arriving from two
sources under two URLs is listed **once** (de-duplicated on the normalised headline).

### Severity

`UGLY` / `BAD` / `GOOD`, from whole-word keyword lists in `app/news/search.py`. Whole
words matter: substring matching turned "firm" into an FIR and "afraid" into a raid,
which mislabels a company on the desk's screen. Hard-adverse beats a positive; a clear
positive beats a routine watch word.

## Configuration

These were the `ATLAS_*` keys in the prototype's `atlas_config.json`. They are environment
variables now — set them in `deploy/compose/.env`, which is git-ignored, so **the SMTP key
never reaches the repo, a zip or an image**.

| Variable | Default | What it does |
|---|---|---|
| `PULSE_SMTP_HOST` | *(blank)* | SMTP relay. **Blank = email disabled**; search still works |
| `PULSE_SMTP_PORT` | `587` | 587 = STARTTLS, 465 = implicit SSL |
| `PULSE_SMTP_USER` | *(blank)* | SMTP login |
| `PULSE_SMTP_PASS` | *(blank)* | SMTP key — never the account password |
| `PULSE_SMTP_FROM` | `= USER` | the From: address |
| `PULSE_SMTP_FROM_NAME` | `PRISM Notification` | display name on the From: header |
| `PULSE_DISABLE_GDELT` | `0` | `1` skips GDELT (Google News + Bing still answer) |
| `PULSE_SCHEDULER_ENABLED` | `true` | `false` stops recurring digests firing |
| `PULSE_SCHEDULE_FILE` | `/data/pulse/schedules.json` | on the `pulsedata` volume |

Email is **optional**. With SMTP unset, `GET /pulse/v1/news/config` reports
`{"email": false}` and every email route answers 400 with *"Email is not configured"* —
a state the dialogs show plainly, rather than a failure discovered at send time.

### Setting up e-mail (Brevo, the recommended relay)

1. Create a Brevo account; under **Senders, Domains & Dedicated IPs ▸ Domains**
   authenticate `evamfinance.com` (add the DKIM + SPF records it shows).
2. Under **SMTP & API ▸ SMTP**, create an SMTP key.
3. Put the login and key in `deploy/compose/.env`:

   ```
   PULSE_SMTP_HOST=smtp-relay.brevo.com
   PULSE_SMTP_PORT=587
   PULSE_SMTP_USER=xxxxx@smtp-brevo.com
   PULSE_SMTP_PASS=<the SMTP key>
   PULSE_SMTP_FROM=news@evamfinance.com
   PULSE_SMTP_FROM_NAME=PRISM Notification
   ```

4. Recreate the service — **a container captures its environment when it is created, so
   a restart is not enough**:

   ```bash
   docker compose -f deploy/compose/docker-compose.yml up -d --force-recreate pulse
   ```

5. Prove it before relying on it: Tools ▸ News Radar ▸ **send a test email**
   (`POST /pulse/v1/news/email-test`). It reports the address it sent *from*.

Gmail works too (`smtp.gmail.com:587`) but needs an app password, and Google rate-limits
bulk sends — a transactional relay is the better fit for digests.

## Schedules

Daily or weekly, per tenant, stored as JSON on the `pulsedata` volume (a handful of rows
does not earn a schema and a migration).

**A restart never fires a catch-up digest.** A schedule whose slot passed while the
service was down is re-anchored to its next future slot — otherwise every deploy would
email whoever is on the recipient list, which is the fastest way to teach a desk to
ignore its own alerts. The scheduler also re-arms a schedule *before* running it, so a
slow or failing run is never retried in a tight loop against an inbox.

Firms with nothing to report are left out of a digest rather than listed as empty.

## The routes

All behind the gateway's `/pulse` prefix; PULSE's own front door accepts only the key the
gateway injects (`PULSE_API_KEYS`), so nothing reaches it directly from a browser.

| Route | Purpose |
|---|---|
| `GET /v1/news/search?q=&from=&to=` | search; returns `{articles:[{title,url,source,when,via,severity}]}` |
| `GET /v1/news/config` | is email configured, is GDELT on, is the scheduler running |
| `POST /v1/news/email` | search a term and email the digest |
| `POST /v1/news/email-digest` | email a digest the caller already assembled (an all-firms sweep) |
| `POST /v1/news/email-test` | send a test email |
| `GET /v1/news/schedules` | this tenant's recurring digests |
| `POST /v1/news/schedules` | create one |
| `POST /v1/news/schedules/delete` | delete one |
| `POST /v1/news/schedules/run` | run one now |

`/v1/scan`, `/v1/items` and `/v1/digest` are unchanged: that is the *watchlist* half of
PULSE, which files intel into the Register against the companies on the book. The News
Radar is the *ad-hoc* half — a person asking about one name, right now.

## Load and cost

A search is cached for 15 minutes (`PULSE_SEARCH_CACHE_TTL_S`) and concurrent requests
for the same term are **coalesced onto one fetch** — an all-firms sweep of 300 companies,
run from three desks at once, costs one set of upstream calls, not three. The upstream
pool is bounded (`PULSE_UPSTREAM_CONCURRENCY`, default 8) so a sweep cannot fan out
without limit.
