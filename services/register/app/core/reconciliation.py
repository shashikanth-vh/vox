"""The single operational-reconciliation predicate.

Reconciliation defines THREE distinct operational classes for an imported record:

* ``reconciliation_status IS NULL``  — fully reconciled / never flagged → operationally complete.
* ``'Required'``                     — still incomplete, unresolved → hidden.
* ``'Waived'``                       — an incomplete record a senior authority DELIBERATELY chose
  to keep; it is a governed exception, NOT fully reconciled — so it is ALSO hidden from routine
  operational reads/exports/counts/automation by default, and only surfaced under an explicit
  Admin/Management inclusion (or in the ``/v1/reconciliation`` work queue).

So the operational predicate below hides EVERY still-flagged record (``Required`` and ``Waived``)
— only a NULL flag is operationally complete. This stops a waived-but-incomplete disbursement-track line
from silently entering disbursed totals or triggering downstream (e.g. Advaya) processing. The two
classes remain distinguishable by the flag value for anyone who explicitly asks to see them.

Centralised here so every read path applies the SAME rule. Two forms are provided because the CRUD
layer works with ORM models and the export layer with Core ``Table`` objects."""

from __future__ import annotations

from typing import Any

# Statuses that hide a record from routine operational reads (only a NULL flag is complete).
HIDDEN_STATUSES = ("Required", "Waived")


def model_exclusion(model: Any) -> Any | None:
    """ORM form: a condition matching only operationally-complete rows (flag IS NULL), or None if
    the model has no reconciliation flag."""
    col = getattr(model, "reconciliation_status", None)
    return col.is_(None) if col is not None else None


def table_exclusion(table: Any) -> Any | None:
    """Core-Table form: a condition matching only operationally-complete rows, or None."""
    col = table.c.get("reconciliation_status")
    return col.is_(None) if col is not None else None
