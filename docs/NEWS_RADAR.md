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

### Triage — `app/news/triage.py`

Four tiers, in the desk's own words. PULSE is the authority: every article it returns
carries its `severity`, `category` and (for a context flip) the `reason`, and the screen
renders that rather than re-deciding, so a scheduled digest and a live search can never
disagree about the same headline. The UI keeps a mirror of the rules only for answers that
did **not** come from PULSE — the direct-GDELT hop, a public proxy, an offline build.

| Tier | What it is |
|---|---|
| `UGLY` | hard-adverse — fraud, enforcement, insolvency. Includes the euphemisms recovery is actually reported by: SARFAESI, NCLT, provisional attachment, lookout circular, DGGI, forensic audit |
| `BAD` | the stress ladder — SMA-0/1/2, restructuring, OTS, covenant/DSCR/DSRA breach, promoter pledge, discom dues, auditor churn, qualified opinion — plus routine litigation |
| `POLICY` | tariff and regulatory moves that name **no company at all** |
| `GOOD` | genuine wins |

**The stress ladder is kept separate from the routine watch words** so it outranks a
positive verb. "Promoter pledge *rises* to 62 per cent" contains "rises"; a rule that lets
any cheerful word win files a distress signal as good news.

#### Polarity is not a property of the headline

"Raises fresh debt" is a win for a name we are chasing and a warning about a name we have
already lent to — the borrower is levering up somewhere else, ahead of us. Six patterns
(fresh debt, OTS, share pledge, an outsized order, a stake sale, an auditor change) flip to
`BAD` / `context-review` when the caller passes `exposure=live`, each carrying the sentence
that explains the flip:

> *An OTS means a lender took a haircut — a default event, not a win.*

The **caller** decides, because the caller holds the book: the screen passes
`exposure=live` for any firm with a lending line that is not Rejected or On Hold. Triage is
applied on the way out of the cache, so one fetch serves both readings. Nothing is
auto-filed as good on a borrower.

#### Two guards

Recall without precision is a radar the desk stops reading.

- **Negation** — a negator within 70 characters *before* the keyword suppresses it, so
  "cleared of siphoning", "acquitted" and "clean chit" do not read as UGLY. The window
  matters: a "cleared" about a different matter later in the sentence must not mask a real
  arrest.
- **Word sense** — "charge" means a criminal charge, except beside "EV charging station",
  which is half this book. Same for "by default", "doing fine", "strike price". Every match
  is checked, not just the first: "EV charging firm arrested over loan fraud" is still UGLY.

Phrases match however they are punctuated, so "one-time settlement" and "one time
settlement" are the same rule.

### The policy sweep — the risk that never names the firm

A state tariff order, an ALMM revision, a change to open-access charges: none of these
mention a borrower, yet each re-prices whole exposures at once. **⚡ Policy** on a firm's
card asks each theme *against that firm's state* and files what comes back against the
firm, so triage shows which exposure the policy move touches. Themes come from
`/v1/news/config` (`policy_themes`) so there is one list, not a copy per client. A firm with
no State set is skipped — policy risk is state-scoped.

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
| `GET /v1/news/search?q=&from=&to=&exposure=` | search; returns `{articles:[{title,url,source,when,via,severity,category,reason?}], sources:[…]}`. `exposure=live` turns on the context flip |
| `GET /v1/news/config` | is email configured, is GDELT on, is the scheduler running, and the `policy_themes` a sweep uses |
| `POST /v1/news/email` | search a term and email the digest |
| `POST /v1/news/email-digest` | email a digest the caller already assembled (an all-firms sweep) |
| `POST /v1/news/email-test` | send a test email |
| `GET /v1/news/schedules` | this tenant's recurring digests |
| `POST /v1/news/schedules` | create one |
| `POST /v1/news/schedules/delete` | delete one |
| `POST /v1/news/schedules/run` | run one now |

### Reading an empty search

Every search reports how each upstream fared:

```json
"sources": [{"name": "Google News", "ok": true,  "count": 7},
            {"name": "GDELT",       "ok": false, "count": 0, "error": "the source did not answer in time"},
            {"name": "Bing News",   "ok": true,  "count": 3}]
```

One source down is normal — the others answer. When **every** source fails the body also
carries a top-level `error`, and the screen shows that sentence instead of an empty list.
This is the difference between *this firm is not in the news* and *this container cannot
reach the news*, which are otherwise the same zero and the reason an all-firms sweep could
report "0 items" while nothing was actually working. A total outage is never cached, so the
next search goes back out to the network rather than replaying the failure for 15 minutes.

The desk searches the **trading name**, not the legal one: the register holds "Avana
Capital Private Limited" and the radar asks for "Avana Capital", because no headline prints
the suffix. Watch terms you add per firm are searched exactly as typed.

`/v1/scan`, `/v1/items` and `/v1/digest` are unchanged: that is the *watchlist* half of
PULSE, which files intel into the Register against the companies on the book. The News
Radar is the *ad-hoc* half — a person asking about one name, right now.

## Load and cost

A search is cached for 15 minutes (`PULSE_SEARCH_CACHE_TTL_S`) and concurrent requests
for the same term are **coalesced onto one fetch** — an all-firms sweep of 300 companies,
run from three desks at once, costs one set of upstream calls, not three. The upstream
pool is bounded (`PULSE_UPSTREAM_CONCURRENCY`, default 8) so a sweep cannot fan out
without limit.
