# The mailbox reader — conversations from your work mail, on the company's timeline

Release 362. A person connects their evamfinance.com mailbox to PRISM once. From
then on PRISM reads it every hour, read-only, keeps the conversations with an
outside party, attaches each to a company, reads what it means, and files one
interaction per conversation on that company's timeline at the mail's own date.
What it cannot place waits for a person in a review queue. Opt-in: nobody's mail is
read until they connect it, and they can disconnect any time.

## For the person

* **Connect.** Account menu (top right) → **My mailbox** → choose how far back to
  read (today onwards, or 3 / 6 / 12 / 24 months) → **Connect my mailbox**. Google's
  own screen asks once for *read your email messages*; one Allow. Today also shows a
  one-line invitation until you connect, with *Not now* (hidden thirty days).
* **What is read.** Only conversations with at least one outside party. Mail where
  everyone is on evamfinance.com, newsletters, notifications, automated senders, and
  anything you label **Private** or **HR** in Gmail are never read. Spam and trash are
  skipped.
* **What is kept.** The extract (summary, what was discussed, decisions, asks,
  amounts and terms, next step, one short quote) and who took part. Not the
  mailbox: PRISM never stores your mail, and it can never send, delete or label
  anything (the permission is read-only).
* **Where it lands.** On the company's live deal, else its active lead, else the
  company itself, as an interaction of type *Email / Written Correspondence* dated by
  the latest message, under your name. Company 360 and the lead and deal timelines
  show it like a logged call. Visibility follows the usual scoping of leads and deals.
* **My mailbox** shows when it was last read, how many conversations were seen, filed,
  waiting for you or left out and why, with **Read now** and **Disconnect**.
  Disconnecting deletes the token; what was filed stays.
* **Reconnect** appears, for you alone, when Google stops accepting the permission
  (a password reset, the app revoked from your Google account, the account
  suspended). Nothing already filed is touched.

## The review queue — Tools → Mail review queue

A conversation the reader could not attach with confidence (no known domain, no
company name in it, or a name match too weak to act on) waits here with its
subject, the outside parties, a short excerpt and the reader's best guess. Pick
the company and **File on this company**; with *Remember the domain* ticked (the
default) the outside party's domain is added to the company's **E-mail domains** on
the client master, so the next conversation from them files itself. **Ignore**
drops it for good. Each person sees their own mailbox's queue; Admin and Management
see everyone's. Entries older than 60 days fall off.

## How it attaches a conversation to a company

| rule | confidence |
|---|---|
| an outside party writes from a domain on the company's **E-mail domains** (or a sub-domain of it) | 0.95 |
| an outside party's address is a contact e-mail the Register knows | 0.90 |
| the company's name or a news alias is in the subject | 0.70 |
| the company's name is in the conversation's text | 0.55 |

Public mail domains (gmail.com, yahoo, outlook, …) never count as a domain. Below
`PULSE_MAIL_MIN_CONFIDENCE` (0.6) the conversation goes to the queue with the best
guess attached. A conversation that grows after it was filed keeps the company it
was filed under.

## What is filed, exactly once

The conversation's identity is the first message's `Message-ID` header, which is the
same in every recipient's mailbox (Gmail's thread ids are not). One interaction is
filed per conversation, with an `Idempotency-Key` built from that identity and the
latest message, so a retry or a second connected mailbox never duplicates it. When
new messages arrive on a filed conversation, a continuation is filed ("Re: …", noting
it continues an earlier filing) dated by the newest message. A conversation whose
note carries a RED, AMBER or GREEN signal also files an **external intelligence** row
(type Email) beside the News Radar's findings, acknowledged the same way.

An interaction dated before a lead's last touch adds history only: the lead's
last-interaction date and next action are updated by an interaction at or after the
last touch, never rewound by a backfill.

## Set-up, once

1. **Google.** In Google Cloud, the OAuth client the login button already uses
   (`GOOGLE_SSO_CLIENT_ID`) needs its **client secret** on the box, and the consent
   screen published as **Internal** to the evamfinance.com workspace. In Testing
   mode Google expires every refresh token after seven days and people would be asked
   weekly; an Internal app needs no Google review for the read-only Gmail scope. Add
   the Gmail API to the project if it is not enabled.
2. **`.env`** in `deploy/compose/`:
   ```
   GOOGLE_SSO_CLIENT_SECRET=<the client's secret>
   # optional
   PULSE_MAIL_ENGINE=auto          # auto | anthropic:<model> | sarvam:<model> | stub
   PULSE_MAIL_TOKEN_KEY=           # blank = a key file generated on the pulse volume
   PULSE_MAIL_INTERNAL_DOMAINS=evamfinance.com
   PULSE_MAIL_EXCLUDE_LABELS=Private,HR
   PULSE_MAIL_ENABLED=true
   ```
   The model is Claude when `ANTHROPIC_API_KEY` is set, else Sarvam with
   `SARVAM_API_KEY`, else the offline stub (subject and the latest message, no
   signal). One model call per conversation, about 2–4k tokens each.
3. `./prism-deploy.sh upgrade …` then recreate `pulse`, `gateway`, `register`, `ui`.
   Migration 0031 adds `email_domains` to the client master.
4. **Tell people.** Reading a work mailbox is a company decision as much as a
   personal one: a short note on what is read, what is kept and who sees it, before
   the first person connects.

## Settings

| `.env` | default | meaning |
|---|---|---|
| `PULSE_MAIL_ENABLED` | true | off = no Connect, nothing read |
| `PULSE_MAIL_ENGINE` | auto | the model that reads a conversation |
| `PULSE_MAIL_INTERVAL_S` | 3600 | each mailbox is read this often |
| `PULSE_MAIL_MAX_THREADS_PER_RUN` | 150 | per mailbox per pass; the rest wait an hour |
| `PULSE_MAIL_BACKFILL_PAGES_PER_RUN` | 3 | history pages (100 conversations each) per pass |
| `PULSE_MAIL_INTERNAL_DOMAINS` | evamfinance.com | who counts as "us" |
| `PULSE_MAIL_EXCLUDE_LABELS` | Private,HR | Gmail labels never read |
| `PULSE_MAIL_MIN_CONFIDENCE` | 0.6 | below it, the queue |
| `PULSE_MAIL_QUEUE_DAYS` | 60 | queue entries older than this fall off |
| `PULSE_MAIL_TOKEN_KEY` | (key file) | Fernet key for the tokens at rest |

## The routes (through the gateway, `/pulse/v1/mail/…`)

| route | who |
|---|---|
| `GET /me`, `POST /connect`, `POST /disconnect`, `POST /read-now` | the signed-in person, their own mailbox |
| `GET /queue` | the person's own items; Admin / Management everyone's |
| `POST /queue/attach`, `POST /queue/ignore` | `log_interaction` gate; own items, or Admin / Management |
| `GET /coverage` | `edit_employee` gate: Admin / Management |

The reader's memory is `/data/pulse/mail.json` on the pulse volume (connections,
what was filed, the queue) with the tokens encrypted; `mail.key` beside it is the
key when `PULSE_MAIL_TOKEN_KEY` is blank. Neither is in any backup set by default:
a lost volume means people connect again, and what was filed is in the Register.
