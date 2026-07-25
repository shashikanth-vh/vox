"""Pure aggregation helpers — plain functions over plain dicts.

Everything here takes lists of Register rows (already-fetched JSON dicts) and returns
JSON-ready summaries. No I/O, no framework imports — which is what makes these
trivially unit-testable and safe for a newcomer to extend: if your function turns a
list of dicts into a dict of numbers, it belongs here.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

Row = dict[str, Any]


def count_by(rows: list[Row], field: str, empty_label: str = "—") -> dict[str, int]:
    """``{"Diligence": 4, "Sanctioned": 2, ...}`` — the universal dashboard widget."""
    counter = Counter((row.get(field) or empty_label) for row in rows)
    return dict(counter.most_common())


def sum_of(rows: list[Row], field: str) -> float:
    """Sum a numeric column, treating missing/None as 0. Rounded for display."""
    return round(sum(float(row.get(field) or 0) for row in rows), 2)


def leads_summary(rows: list[Row]) -> Row:
    return {
        "total": len(rows),
        "by_status": count_by(rows, "status"),
        "by_temperature": count_by(rows, "temperature"),
        "by_source": count_by(rows, "source"),
    }


def deals_summary(rows: list[Row]) -> Row:
    return {
        "total": len(rows),
        "by_stage": count_by(rows, "stage"),
        "by_product_type": count_by(rows, "product_type"),
        "lending_lines": sum(1 for r in rows if r.get("is_lending")),
        "syndication_lines": sum(1 for r in rows if r.get("is_syndication")),
        "asset_mon_lines": sum(1 for r in rows if r.get("is_asset_mon")),
    }


def lending_summary(rows: list[Row]) -> Row:
    return {
        "total": len(rows),
        "by_stage": count_by(rows, "stage"),
        "by_pending_with": count_by(rows, "pending_with"),
        "amount_cr": sum_of(rows, "amount_cr"),
        "disbursed_cr": sum_of(rows, "disbursed_amount"),
    }


def syndication_summary(rows: list[Row]) -> Row:
    return {
        "total": len(rows),
        "by_status": count_by(rows, "status"),
        "by_priority": count_by(rows, "priority"),
        "amount_cr": sum_of(rows, "amount_cr"),
    }


def asset_mon_summary(rows: list[Row]) -> Row:
    return {
        "total": len(rows),
        "by_status": count_by(rows, "status"),
        "by_nature": count_by(rows, "nature"),
        "indicative_value_cr": sum_of(rows, "indicative_value_cr"),
        "size_mw": sum_of(rows, "size_mw"),
    }


def intel_summary(rows: list[Row]) -> Row:
    open_rows = [r for r in rows if not r.get("is_dismissed")]
    return {
        "open": len(open_rows),
        "by_signal": count_by(open_rows, "signal"),
        "unacknowledged_red": sum(
            1 for r in open_rows
            if r.get("signal") == "RED" and not r.get("acknowledged_at")),
    }


# --------------------------------------------------------------------------- #
# The "Today" view — what needs a human right now
# --------------------------------------------------------------------------- #
def _due(value: str | None, today: date, horizon_days: int = 0) -> bool:
    """Is an ISO date due (<= today + horizon)? Unparseable/missing dates are not due."""
    if not value:
        return False
    try:
        return date.fromisoformat(value[:10]).toordinal() <= today.toordinal() + horizon_days
    except ValueError:
        return False


def leads_due_today(rows: list[Row], today: date) -> list[Row]:
    """Leads whose next action is due (or overdue), soonest first."""
    due = [r for r in rows
           if r.get("status") == "Active" and _due(r.get("next_action_date"), today)]
    due.sort(key=lambda r: r.get("next_action_date") or "")
    return [{"lead_id": r["id"], "company": r.get("company"), "rm": r.get("rm"),
             "next_action": r.get("next_action"),
             "next_action_date": r.get("next_action_date"),
             "temperature": r.get("temperature")} for r in due]


def lender_chases(rows: list[Row]) -> list[Row]:
    """Syndication lenders we are waiting on: approached but no response logged yet."""
    waiting = [r for r in rows if not r.get("response_date")
               and (r.get("status") or "").lower() not in ("sanctioned", "rejected", "dropped")]
    waiting.sort(key=lambda r: r.get("chased_date") or "")
    return [{"lender_row_id": r["id"], "syndication_id": r.get("syndication_id"),
             "lender_name": r.get("lender_name"), "status": r.get("status"),
             "last_chased": r.get("chased_date")} for r in waiting]


def monitoring_due(rows: list[Row], today: date, horizon_days: int = 7) -> list[Row]:
    """Covenants / submissions due within the horizon and not yet submitted."""
    due = [r for r in rows if not r.get("submitted_date")
           and _due(r.get("due_date"), today, horizon_days)]
    due.sort(key=lambda r: r.get("due_date") or "")
    return [{"monitoring_id": r["id"], "entity_id": r.get("entity_id"),
             "record_type": r.get("record_type"), "covenant_name": r.get("covenant_name"),
             "due_date": r.get("due_date")} for r in due]
