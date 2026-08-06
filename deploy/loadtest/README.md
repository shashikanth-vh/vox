# Load testing PRISM — the 1,000-user posture

`k6-profile.js` generates the traffic real browsers generate, authenticated as real
identities through Dex → gateway OIDC — not an anonymous hammer:

| Scenario   | What it simulates                                   | Rate at USERS=1000 |
|------------|-----------------------------------------------------|--------------------|
| `bell`     | every signed-in user's notification bell (45s poll) | ~22 req/s          |
| `today`    | ~30% of users sitting on Today (60s poll)           | ~5 req/s           |
| `browse`   | ~10% of users touching the book per minute          | ~2×2 req/s         |
| `bookings` | the LMS pair's booking queue                        | 2 req/min          |
| `writes`   | optional: interactions logged (marked `LOADTEST`)   | `WRITE_RATE`/min   |

## Run it

From any machine that can reach the VM (k6 via Docker, nothing to install):

```bash
cd deploy/loadtest
docker run --rm -i --network host \
  -e BASE=https://192.168.44.128:8443 \
  -e USERS=1000 -e DURATION=10m \
  grafana/k6 run - < k6-profile.js
```

Knobs (all env vars): `BASE`, `USERS` (default 1000), `DURATION` (default 10m),
`PASSWORD` (default `prism`), `WRITE_RATE` (interactions/minute, default 0 =
read-only — safe against any environment).

Ramp discipline: do a `USERS=100 DURATION=2m` smoke first, then 500, then 1000.
A failure at 100 is a bug; a failure only at 1000 is capacity.

## The success bar

The script exits non-zero when the bar is missed (so it can gate a pipeline):

- error rate < 1%
- bell / bookings reads p95 < 500 ms
- book browse p95 < 800 ms
- Today's pending list p95 < 1.5 s (Temporal visibility sits behind it)

## What to watch on the VM while it runs

```bash
# container CPU/memory — who saturates first
docker stats

# Postgres: connections in use vs the 200 cap, and anything stuck waiting
docker compose exec postgres psql -U prism -c \
  "SELECT state, count(*) FROM pg_stat_activity GROUP BY state;"
docker compose exec postgres psql -U prism -c \
  "SELECT pid, wait_event_type, wait_event, left(query,60) FROM pg_stat_activity
   WHERE wait_event IS NOT NULL AND state='active';"

# the edge: status-code mix and slow requests
docker compose logs --tail 200 nginx | grep -v ' 200 '
```

Reading the result:

- **p95 rises but errors stay 0** → a queue is forming: check `docker stats` for the
  saturated container; more workers/replicas for that service is the usual answer.
- **`http_req_failed` climbs with 502/504** → the edge is timing out on a backend:
  the nginx log names which upstream; that service's logs name why.
- **Postgres `active` connections pinned at pool ceiling (30/service)** → queries are
  slow, not traffic heavy: look for the missing index before adding hardware.
- **Dex sign-in failures at start** → the run authenticates 7 identities once in
  setup; if that fails nothing else ran — check the `/dex/` edge route first.

## Cleaning up after a write-mix run

The write mix marks everything it creates:

```sql
DELETE FROM interactions WHERE summary LIKE 'LOADTEST %';
```

Run read-only (`WRITE_RATE=0`, the default) against anything you care about;
point write runs at a staging copy when possible.
