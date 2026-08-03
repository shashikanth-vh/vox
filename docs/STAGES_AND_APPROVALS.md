# Who moves a stage, and how an approver acts — the two questions

## 1. Are stages moved automatically, or does the user set them?

**Both — and which one applies is a property of the stage, not a preference.** Every
tracker stage falls into exactly one of two classes:

### GOVERNED stages — the platform moves them, a human never can

A governed stage is the *record of a decision that already happened*. Typing it by
hand would let someone assert an outcome that never occurred, so the register refuses
it (422) no matter who asks — the dropdown will show the value, the write will not
land. It moves only when its evidence exists:

| Stage | Moved by | Refused by hand because |
|---|---|---|
| Lending **Sanctioned** | the structuring workflow, after the committee decision | it asserts a Credit Committee approved this facility |
| Lending **CP/CS Completed** | after the CP/CS checklist is checker-approved + executed agreement filed | it asserts conditions precedent were verified |
| Lending **Disbursed** | the FIRST disbursement tranche (Advaya's callback, or the manual attestation) | it asserts money actually moved |
| Deal **Closed Won / Closed Lost** | `POST /v1/deals/{id}/close` (open items validated, note mandatory) | it asserts nothing was left open |
| Syndication / AM terminal statuses | their mandate decisions | same: an authority decided |

### OPERATIONAL stages — the user sets them, freely

The rest are the team's own working state — **Data Awaited, Diligence, Note
Circulated, Documentation, On Hold, Rejected, Ready for Disbursement** on lending, and
the deal funnel stages. Pick them in the UI dropdown (the change is stamped and
appended to `stage_history` with who and when); the platform only enforces that
transitions stay in order, so `Note Circulated → CP/CS Completed` is refused — not
because the stage is governed, but because you skipped the steps in between.

That is exactly the 422 you saw: `Lending.stage may not move 'Note Circulated' →
'CP/CS Completed' (allowed: Diligence, On Hold, Rejected, Sanctioned)`. The line was
still waiting on the committee.

**Rule of thumb:** if a stage records that *somebody decided something*, the workflow
writes it. If it records *where the team is up to*, the user writes it.

## 2. The approver's three verbs — approve is never the only option

Every human gate offers the **triad**, and `GET /v1/workflows/pending` now returns all
three URLs per item, so a UI renders exactly the buttons the platform accepts:

```jsonc
{
  "kind": "cpcs-checklist", "subject_id": "…", "status": "Completed",
  "stage": "Awaiting checker approval",
  "approve_url": "/v1/workflows/cpcs-checklists/{id}/approve",
  "return_url":  "/v1/workflows/cpcs-checklists/{id}/return",   // amend & come back
  "reject_url":  "/v1/workflows/cpcs-checklists/{id}/reject"    // terminal
}
```

For a *parked run* (conversion, committee, syndication, AM) the return lane is
run-control: `return_url` = `/v1/workflows/{id}/control` with `{action:"return"}`.

| Verb | Terminal? | Note | What happens next |
|---|---|---|---|
| **Approve** | yes | optional | the item advances; the governed stage moves |
| **Return** | **no** | **mandatory** | goes BACK to the maker/requester; they revise and resubmit, and it re-enters this queue |
| **Reject** | yes | **mandatory** | this attempt is dead; a revival is a fresh version/cycle, never a resurrection |

## 3. The complete loop, from the UI

**Today tab → "Workflow approvals"** is the approver's queue. It now lists *everything*
they can act on (parked runs AND the CP/CS + handover checker queues), each row
carrying **Approve · Return · Reject**.

```
MAKER                                     CHECKER / APPROVER
─────                                     ──────────────────
prepares (CP/CS checklist v1,
handover package, credit note)
        │
        ▼  the item appears in the approver's Today queue
                                          opens it → three buttons
                                          ├─ Approve  → flow advances, stage moves
                                          ├─ Return   → note required
                                          └─ Reject   → note required, terminal
        ◄─────────────────────────────────┘ (Return)
amends: prepares the NEXT version
(checklist v2 / re-prepared package /
revised credit note + resubmit)
        │
        ▼  back in the approver's queue as v2 → Approve → done
```

Nothing about that loop is Postman-specific: the maker's "prepare/resubmit" actions are
the same endpoints the UI's own screens call, and the approver's three buttons post to
the three URLs above. Postman simply scripts the same calls in a fixed order.

**Where each side acts in the UI**

| Step | Screen |
|---|---|
| Maker prepares CP/CS / handover / credit note | Lending tab → the line's actions |
| Item reaches the approver | Today → Workflow approvals (auto-refreshes) |
| Approver decides | the row's Approve / Return / Reject → dialog (note enforced) |
| Maker sees a returned item | Today → their own raised items show the returned state |
| Maker resubmits | prepare the next version — it re-enters the queue |
| Watch the stage move | Lending tab → STAGE column + stage history |
