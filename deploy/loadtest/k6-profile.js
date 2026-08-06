// PRISM load profile — 1,000-user posture, the traffic real browsers generate.
//
// Mirrors the actual clients, not a synthetic hammer:
//   · every signed-in user's BELL polls /v1/notifications every ~45s
//   · a share of users sit on Today: /orchestrator/v1/workflows/pending every ~60s
//   · the LMS pair polls the booking queue
//   · people browse the book (lending list, company typeahead)
//   · an optional WRITE mix logs interactions (clearly marked "LOADTEST")
//
// Run (from any machine that can reach the VM; k6 via docker, no install):
//   docker run --rm -i --network host \
//     -e BASE=https://192.168.44.128:8443 -e USERS=1000 -e DURATION=10m \
//     grafana/k6 run - < k6-profile.js
//
// Knobs (env): BASE, USERS (default 1000), DURATION (10m), PASSWORD (prism),
//              WRITE_RATE (interactions/minute, default 0 = read-only).
//
// Success bar: http_req_failed < 1%, read p(95) < 500ms — the thresholds below
// make k6 exit non-zero when the bar is missed, so this can gate a pipeline.

import http from 'k6/http';
import { check, fail } from 'k6';

const BASE = __ENV.BASE || 'https://192.168.44.128:8443';
const USERS = Number(__ENV.USERS || 1000);
const DURATION = __ENV.DURATION || '10m';
const PASSWORD = __ENV.PASSWORD || 'prism';
const WRITE_RATE = Number(__ENV.WRITE_RATE || 0);

// The demo roster. Every request authenticates as a REAL identity through Dex →
// gateway OIDC, exactly like a browser. Approver-shaped polls use approver tokens.
const EVERYONE = [
  'divya.rao@evamfinance.com', 'arun.menon@evamfinance.com',
  'priya.nair@evamfinance.com', 'lakshmi.narayanan@evamfinance.com',
  'suresh.kumar@evamfinance.com', 'karthik.reddy@evamfinance.com',
  'rohit.sharma@evamfinance.com',
];
const APPROVERS = ['divya.rao@evamfinance.com', 'arun.menon@evamfinance.com'];
const LMS = ['suresh.kumar@evamfinance.com', 'karthik.reddy@evamfinance.com'];

export const options = {
  insecureSkipTLSVerify: true,           // the VM's self-signed edge cert
  thresholds: {
    http_req_failed: ['rate<0.01'],
    'http_req_duration{kind:bell}': ['p(95)<500'],
    'http_req_duration{kind:today}': ['p(95)<1500'],   // Temporal visibility behind it
    'http_req_duration{kind:browse}': ['p(95)<800'],
    'http_req_duration{kind:bookings}': ['p(95)<500'],
  },
  scenarios: {
    // Every signed-in user's bell: USERS polls spread over each 45s window.
    bell: {
      executor: 'constant-arrival-rate', exec: 'bell',
      rate: Math.max(1, Math.round(USERS / 45)), timeUnit: '1s',
      duration: DURATION, preAllocatedVUs: 50, maxVUs: 200,
    },
    // ~30% of users sitting on Today, refreshing every 60s.
    today: {
      executor: 'constant-arrival-rate', exec: 'today',
      rate: Math.max(1, Math.round((USERS * 0.3) / 60)), timeUnit: '1s',
      duration: DURATION, preAllocatedVUs: 20, maxVUs: 100,
    },
    // Browsing the book: ~10% of users touching a list/typeahead per minute.
    browse: {
      executor: 'constant-arrival-rate', exec: 'browse',
      rate: Math.max(1, Math.round((USERS * 0.1) / 60)), timeUnit: '1s',
      duration: DURATION, preAllocatedVUs: 20, maxVUs: 100,
    },
    // The servicing pair's booking queue, each every 60s.
    bookings: {
      executor: 'constant-arrival-rate', exec: 'bookings',
      rate: 2, timeUnit: '60s',
      duration: DURATION, preAllocatedVUs: 2, maxVUs: 10,
    },
    ...(WRITE_RATE > 0 ? {
      writes: {
        executor: 'constant-arrival-rate', exec: 'writes',
        rate: WRITE_RATE, timeUnit: '60s',
        duration: DURATION, preAllocatedVUs: 5, maxVUs: 20,
      },
    } : {}),
  },
};

function signIn(email) {
  const r = http.post(`${BASE}/dex/token`, {
    grant_type: 'password', client_id: 'prism',
    scope: 'openid email profile', username: email, password: PASSWORD,
  });
  if (r.status !== 200) fail(`Dex sign-in failed for ${email}: ${r.status} ${r.body}`);
  const tok = r.json('id_token') || r.json('access_token');
  if (!tok) fail(`Dex returned no id_token for ${email}`);
  return tok;
}

// One sign-in per identity for the whole run — like a browser session. Tokens are
// short-lived; a run longer than the Dex TTL (default 24h) would need re-auth.
// Each identity then fires ONE warm-up request: an identity's first-ever request
// pays a one-time cost (token verification, role-resolution caches) that a real
// user pays at sign-in — without this, a low-rate scenario like `bookings`
// (4 samples in a 2-minute smoke) reports that warm-up AS its p95.
export function setup() {
  const tokens = {};
  for (const email of EVERYONE) {
    tokens[email] = signIn(email);
    http.get(`${BASE}/v1/notifications?unread_only=true&limit=1`,
      { headers: { Authorization: `Bearer ${tokens[email]}` },
        tags: { kind: 'warmup' } });
  }
  // One entity id for the write mix, read as a real user would (typeahead → first hit).
  const h = { Authorization: `Bearer ${tokens[EVERYONE[0]]}` };
  const ents = http.get(`${BASE}/v1/entities?limit=5`, { headers: h });
  const first = ents.status === 200 ? (ents.json('items.0.id') || ents.json('0.id')) : null;
  return { tokens, entityId: first };
}

const pick = (arr) => arr[Math.floor(Math.random() * arr.length)];
const auth = (data, pool) => ({ Authorization: `Bearer ${data.tokens[pick(pool)]}` });

export function bell(data) {
  const r = http.get(`${BASE}/v1/notifications?unread_only=true&limit=20`,
    { headers: auth(data, EVERYONE), tags: { kind: 'bell' } });
  check(r, { 'bell 200': (x) => x.status === 200 });
}

export function today(data) {
  const r = http.get(`${BASE}/orchestrator/v1/workflows/pending`,
    { headers: auth(data, APPROVERS), tags: { kind: 'today' } });
  check(r, { 'pending 200': (x) => x.status === 200 });
}

export function browse(data) {
  const h = auth(data, EVERYONE);
  const list = http.get(`${BASE}/v1/lending?limit=100`,
    { headers: h, tags: { kind: 'browse' } });
  check(list, { 'lending 200': (x) => x.status === 200 });
  const q = pick(['a', 'e', 'in', 'po', 'so']);
  const sug = http.get(`${BASE}/v1/entities?q=${q}&limit=8`,
    { headers: h, tags: { kind: 'browse' } });
  check(sug, { 'typeahead 200': (x) => x.status === 200 });
}

export function bookings(data) {
  const r = http.get(`${BASE}/v1/bookings/pending`,
    { headers: auth(data, LMS), tags: { kind: 'bookings' } });
  check(r, { 'bookings 200': (x) => x.status === 200 });
}

// WRITE mix — clearly marked rows so they can be found and purged afterwards:
//   DELETE FROM interactions WHERE summary LIKE 'LOADTEST %';
export function writes(data) {
  if (!data.entityId) return;
  const r = http.post(`${BASE}/v1/entities/${data.entityId}/interactions`,
    JSON.stringify({
      interaction_type: 'Call',
      occurred_at: new Date().toISOString(),
      summary: `LOADTEST synthetic interaction ${Date.now()}`,
      performed_by: 'loadtest',
    }),
    { headers: { ...auth(data, EVERYONE), 'Content-Type': 'application/json' },
      tags: { kind: 'write' } });
  check(r, { 'interaction 201': (x) => x.status === 201 || x.status === 200 });
}
