# User provisioning via Postman

How to create and manage PRISM users — including the LMS servicing pair — with the
`postman/PRISM_Demo_Users.postman_collection.json` collection, and what to do when you
need a user the collection doesn't ship.

## The two halves of a user

Every working user is TWO records, deliberately separate:

| Half | Lives in | Gives the user | Created by |
|---|---|---|---|
| **Sign-in identity** | The IdP — Dex (`deploy/compose/dex/config.yaml`) in dev, your real IdP (Google) in production | A way to log in and get a verified token | Editing the Dex config (dev) / IdP admin (prod) |
| **Access record** | The Access service (`POST /access/v1/users`) | Roles → permissions under the RBAC matrix | An **Admin**, via Postman or Masters ▸ Employees |

A Dex entry without an Access record signs in with no roles (sees almost nothing); an
Access record without a Dex entry has roles but no way to log in. You need both.

## The demo team (all passwords: `prism`)

| Email | Name | Roles |
|---|---|---|
| `admin@evamfinance.com` | Admin | Admin |
| `divya.rao@evamfinance.com` | Divya Rao | Management |
| `arun.menon@evamfinance.com` | Arun Menon | Credit Head, Deal Analyst |
| `priya.nair@evamfinance.com` | Priya Nair | BDRM, Syn RM, AM RM |
| `lakshmi.narayanan@evamfinance.com` | Lakshmi Narayanan | **LMS Operator** (servicing maker) |
| `suresh.kumar@evamfinance.com` | Suresh Kumar | **LMS Management** (servicing checker) |
| `rohit.sharma@evamfinance.com` | Rohit Sharma | **LMS Operator** — reports to Karthik |
| `karthik.reddy@evamfinance.com` | Karthik Reddy | **LMS Management** |

The LMS pair exists because four-eyes at the booking gate is enforced server-side:
the person who records a disbursement tranche can never approve its booking, so the
demo needs both identities to walk LOS → LMS end to end.

## Running the collection

1. **Import** `postman/PRISM_Demo_Users.postman_collection.json` and the
   `postman/PRISM_Full_Dex.postman_environment.json` environment.
2. **Point the environment at your deployment** — for the demo VM set `baseUrl`,
   `accessUrl`, `orchestratorUrl`, `dexUrl` from `https://localhost:8443` to
   `https://192.168.44.128:8443` (keep the path suffixes: `/access`, `/orchestrator`).
   The edge uses a self-signed certificate, so turn **off** SSL certificate
   verification in Postman's settings.
3. **Run top to bottom** (or Collection Runner). What happens:
   - `POST /dex/token` — signs in as `{{userEmail}}` (the Admin) with
     `{{ssoPassword}}`; stores the ID token every later request presents.
   - `POST /access/v1/users` ×5 — creates the demo trio and the LMS pair with their
     roles. **201** = created, **409** = already there; both pass — the collection is
     idempotent, run it after every fresh database.
   - `POST /v1/internal/people/sync-access` — the Register's people roster catches up
     from Access (dropdowns, scoping and VocX read this half).
   - resolve + `PATCH /v1/people` — sets the reporting lines (Arun and Priya report
     to Divya) and verifies the LMS Operator landed on the roster.

## Adding a user the collection doesn't ship

1. **Give them a sign-in** (dev): add a block to `deploy/compose/dex/config.yaml`
   under `staticPasswords` — copy any existing entry, change `email`, `username`,
   `userID`. The shipped hash encodes the password `prism`; for a different password:
   `python3 -c "import bcrypt; print(bcrypt.hashpw(b'yourpass', bcrypt.gensalt(10)).decode())"`.
   Then `docker compose --profile sso restart dex` (the config is bind-mounted).
2. **Create the Access record** — duplicate any user request in the collection and
   edit the body:

   ```json
   {
     "email": "new.person@evamfinance.com",
     "full_name": "New Person",
     "short_name": "New",
     "is_active": true,
     "roles": ["LMS Operator"]
   }
   ```

   Valid roles (RBAC v3.7): `Admin`, `Management`, `BD Head`, `BDRM`, `Credit Head`,
   `Deal Analyst`, `Syn Head`, `Syn RM`, `AM Head`, `AM RM`, `LMS Operator`,
   `LMS Management`. Stack several in one list for multi-hat users. (`LMS Authorizer`
   is this role's pre-v3.7 name — old grants still resolve through the alias, but new
   creations must use `LMS Management`.)
3. **Re-run the roster sync** request so the Register picks the person up.

## Managing existing users

All Admin-only, same headers as the create requests:

| Action | Request |
|---|---|
| Add a role | `POST {{accessUrl}}/v1/users/{user_id}/roles` — body `{"role": "LMS Management"}` |
| Remove a role | `DELETE {{accessUrl}}/v1/users/{user_id}/roles/{role}` |
| Deactivate / edit | `PATCH {{accessUrl}}/v1/users/{user_id}` — body `{"is_active": false}` |
| Find a user's id | `GET {{accessUrl}}/v1/users?q=<email>` |

Role changes and deactivation bump the user's permissions epoch — already-issued
signed contexts fail revalidation immediately; there is no "log out and back in later"
window on revocation.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Sign-in test fails, no token | `dexUrl` empty or wrong; user missing from Dex config; wrong `ssoPassword`. After editing Dex config: restart the dex container. |
| `Unknown role '…'` on create | Typo, or a pre-v3.7 name — use the list above verbatim (roles are case-sensitive). |
| 403 on the create requests | The signed-in user isn't an Admin — user writes are governance. |
| User signs in but sees nothing | Access record missing (create it), or roles list empty. |
| Roster resolve fails | The sync request didn't run — run `sync-access`, then resolve again. |
| Policy-version drift warnings in logs | Services built from different zips — rebuild all images from one zip (`docker compose build`). |
