# Managing People in Production — the One-Page Runbook

> **All people management happens in Masters ▸ Employees. Nothing else. Ever.**
>
> Postman, DBeaver and the E2E collection are test-bench tools. In production they have
> no role in user management.

Only an **Admin** (or Management) can perform these actions.

## The four actions

| You want to… | You do… | What happens automatically |
| --- | --- | --- |
| **Hire someone** | *Add employee* — fill the form once (e-mail is mandatory: it is the person's identity) | They can sign in, appear in every BDRM/Analyst dropdown, and own records from that moment |
| **Change role / details** | *Edit* — change, save | Sign-in permissions and the roster both update; a role change takes effect live |
| **Someone leaves** | *Delete* — the dialog asks who takes their book | Sign-in revoked instantly · every lead/deal/line they own moves to the successor · their assignments transfer · history stays intact and attributable |
| **Temporary suspension** | *Edit* — switch *Active* off | Locked out instantly, even mid-session; switch it back on when they return |

## Things you never have to do

* Create users anywhere else — the screen provisions everything in one save.
* Clean up after a leaver — the delete dialog will not let a book go ownerless
  without an explicit "no handover" choice.
* Worry about half-created people — if one ever appears (e.g. from a script), the
  screen flags it with a chip and says exactly how to finish it:
  * 🟠 **sign-in only** — can log in but no dropdown offers them → open and save them once.
  * 🔵 **no sign-in** — dropdowns accept them but they cannot log in → *Add employee*
    with the same e-mail.
* Delete history — nothing here erases the past. "Who approved this?" stays
  answerable for the life of every loan.

## The one exception

The **first Admin** of a brand-new deployment is seeded by the installation itself
(someone must be able to sign in before anyone can add anyone). Every person after
them comes through this screen.
