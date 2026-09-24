"""Business columns and record navigation from validated, caller-authorized results."""
from __future__ import annotations

import json
import re

from app.presentation import business_text

_FIELDS = {
    "deal_no": "Group code", "code": "Group code", "lead_no": "Lead number",
    "tracker_no": "Reference", "company": "Company", "name": "Name",
    "display_name": "Company", "legal_name": "Company", "rm": "RM",
    "analyst": "Analyst", "temperature": "Temperature", "stage": "Stage",
    "status": "Status", "product_type": "Product", "amount_cr": "Amount (Cr)",
    "remarks": "Remarks", "notes": "Notes", "sector": "Sector", "lens": "Lens",
    "lender_name": "Lender", "priority": "Priority", "pending_with": "Pending with",
}
_RESOURCES = {"deals", "leads", "entities", "lending", "syndication",
              "syndication_lenders", "asset_monetisation"}
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if not isinstance(value, str | int | float):
        return ""
    text = str(value)
    if business_text(text) != text:
        return ""
    return text[:300] + ("…" if len(text) > 300 else "")


def _code(value):
    if not isinstance(value, str) or not re.fullmatch(r"[\w .-]{1,120}", value) or _UUID.fullmatch(value):
        return None
    return value


def result_tables(execution, plan):
    # Only original authorized reads supply link targets. A result with several
    # contributing deals is an aggregate and has no single record destination.
    deals = {str(row.get("id")): row.get("deal_no") or row.get("code")
             for read in plan.reads if read.resource == "deals"
             for row in execution.datasets.get(read.name, []) if row.get("id")}
    tables = []
    remaining_bytes = 600_000
    for name in execution.result_names:
        if execution.result_shapes[name] == "scalar":
            continue
        rows = execution.datasets[name]
        sources = (execution.result_field_sources or {}).get(name, {})
        columns = []
        for key, origins in sources.items():
            labels = {_FIELDS.get(origin["field"]) for origin in origins
                      if origin.get("resource") in _RESOURCES}
            # Every origin must be an allowed business field, including joined aliases.
            if len(labels) == 1 and None not in labels and len(origins) and all(
                o.get("resource") in _RESOURCES and o.get("field") in _FIELDS for o in origins
            ):
                columns.append((key, next(iter(labels))))
        unique_columns = []
        for key, label in columns:
            if not any(label == other_label and all(row.get(key) == row.get(other_key) for row in rows)
                       for other_key, other_label in unique_columns):
                unique_columns.append((key, label))
        priority = ["Group code", "Company", "Name", "Lead number", "Reference",
                    "Temperature", "Stage", "Status", "RM", "Analyst", "Product"]
        columns = sorted(unique_columns, key=lambda column:
                         priority.index(column[1]) if column[1] in priority else len(priority))
        output = []
        cohorts = (execution.cohort_rows or {}).get(name, [])
        for index, row in enumerate(rows[:2000]):
            links = {str(o.get("field")): row.get(key) for key, origins in sources.items()
                     for o in origins if o.get("resource") == "deals"
                     and o.get("field") in {"deal_no", "code"}}
            code = _code(links.get("deal_no") or links.get("code"))
            if not code and index < len(cohorts):
                ids = {token.split(":", 1)[1] for token in cohorts[index].get("lineage", [])
                       if token.startswith("deals:")}
                if len(ids) == 1:
                    code = _code(deals.get(next(iter(ids))))
            item = {"values": [_cell(row.get(key)) for key, _ in columns]}
            if code:
                item["deal"] = code
            # Match the SSE encoder's ASCII escaping when enforcing its byte budget.
            size = len(json.dumps(item).encode("utf-8"))
            if size > remaining_bytes:
                break
            remaining_bytes -= size
            output.append(item)
        if columns and output:
            tables.append({"title": "Matching records", "columns": [label for _, label in columns],
                           "rows": output, "total": len(rows),
                           "partial": str(execution.completeness) != "COMPLETE"})
    return tables
