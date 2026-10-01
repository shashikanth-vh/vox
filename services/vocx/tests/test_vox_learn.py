"""The learning tool over an export of approved conversations: the model's
report is rebuilt from the edit trail, the scorecard counts what reviewers
changed, and corrections become proposed aliases."""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "evals"))
import vox_learn  # noqa: E402


def _row(rid, report, edits=(), raw=None, fixed=None, engine="default"):
    return {"id": rid, "status": "submitted", "approved_engine": engine,
            "raw_transcript": raw, "corrected_transcript": fixed,
            "structured_report": report, "edits": list(edits)}


def _c(v, conf="high"):
    return {"value": v, "confidence": conf}


def test_the_model_report_is_rebuilt_from_the_earliest_edit():
    approved = {"lending": {"requirement_quantum_cr": _c(5, "n/a"), "existing_bankers": _c("Kotak Mahindra Bank")}}
    edits = [
        {"field_path": "lending.requirement_quantum_cr", "old_value": _c(25, "high"), "new_value": _c(10)},
        {"field_path": "lending.requirement_quantum_cr", "old_value": _c(10), "new_value": _c(5, "n/a")},
        {"field_path": "lending.existing_bankers", "old_value": _c("Kotak Bank", "medium"), "new_value": _c("Kotak Mahindra Bank")},
        {"field_path": "links.entity_id", "old_value": None, "new_value": "x"},
    ]
    m = vox_learn.model_report(_row("a", approved, edits))
    assert m["lending"]["requirement_quantum_cr"] == _c(25, "high")
    assert m["lending"]["existing_bankers"] == _c("Kotak Bank", "medium")
    # the approved report itself is untouched
    assert approved["lending"]["requirement_quantum_cr"]["value"] == 5


def test_the_scorecard_counts_missed_invented_wrong_and_right():
    rows = [
        _row("a", {"lending": {"requirement_quantum_cr": _c(5), "existing_bankers": _c("SBI"),
                               "project_location": _c("Karnataka")}},
             [{"field_path": "lending.requirement_quantum_cr", "old_value": _c(25, "high"), "new_value": _c(5)},
              {"field_path": "lending.existing_bankers", "old_value": _c(None, "n/a"), "new_value": _c("SBI")}]),
        _row("b", {"lending": {"requirement_quantum_cr": _c(8), "existing_bankers": _c(None, "n/a"),
                               "project_location": _c("Tamil Nadu")}},
             [{"field_path": "lending.existing_bankers", "old_value": _c("Nowhere Bank", "medium"), "new_value": _c(None, "n/a")}],
             engine="regional"),
        _row("c", {"lending": {"requirement_quantum_cr": _c(3), "existing_bankers": _c("HDFC Bank"),
                               "project_location": _c(None, "n/a")}}),
    ]
    sc = vox_learn.scorecard(rows)
    assert sc["notes"] == 3 and sc["notes_with_no_field_edits"] == 1
    f = {x["field"]: x for x in sc["fields"]}
    q = f["lending.requirement_quantum_cr"]
    assert q["in_play"] == 3 and q["edited"] == 1 and q["model_wrong"] == 1 and q["right"] == 2
    assert q["confidence_when_edited"] == {"high": 1}
    bk = f["lending.existing_bankers"]
    assert bk["model_missed"] == 1 and bk["model_invented"] == 1 and bk["edited"] == 2
    assert bk["in_play"] == 3                      # a, b (model had a value), c
    assert "lending.project_location" in f and f["lending.project_location"]["edited"] == 0
    assert sc["fields"][0]["field"] == "lending.existing_bankers"   # highest edit rate first
    assert sc["by_engine"]["regional"]["notes"] == 1


def test_corrections_become_ranked_alias_proposals():
    rows = [
        _row("a", {"syndication": {"probable_lenders": _c("Axis Bank, Kotak Mahindra Bank")}},
             [{"field_path": "syndication.probable_lenders",
               "old_value": _c("Access Bank, Kotak Menindra Bank"), "new_value": _c("Axis Bank, Kotak Mahindra Bank")}],
             raw="we spoke to Access Bank and Egypt Burla Finance about it",
             fixed="we spoke to Axis Bank and Aditya Birla Finance about it"),
        _row("b", {"syndication": {"probable_lenders": _c("Aditya Birla Finance")}},
             [{"field_path": "transcript.corrected",
               "old_value": "Egypt Burla Finance will join", "new_value": "Aditya Birla Finance will join"}]),
        _row("c", {"lending": {"existing_bankers": _c("Jana Small Finance Bank")}}),
    ]
    m = vox_learn.mine(rows, min_count=2)
    aliases = m["canonical_aliases"]
    assert aliases.get("Access Bank") == "Axis Bank"
    assert aliases.get("Egypt Burla Finance") == "Aditya Birla Finance"
    assert all(p["count"] >= 2 for p in m["proposals"])
    once = {(p["heard"], p["canonical"]) for p in m["seen_once"]}
    assert ("Kotak Menindra Bank", "Kotak Mahindra Bank") in once
    kept = dict(m["lender_names_kept_by_reviewers"])
    assert kept.get("Jana Small Finance Bank") == 1


def test_the_cli_runs_on_an_export_file(tmp_path, capsys):
    p = tmp_path / "x.jsonl"
    rows = [_row("a", {"lending": {"requirement_quantum_cr": _c(5)}},
                 [{"field_path": "lending.requirement_quantum_cr", "old_value": _c(2, "medium"), "new_value": _c(5)}])]
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    assert vox_learn.main(["scorecard", str(p)]) == 0
    out = capsys.readouterr().out
    assert "lending.requirement_quantum_cr" in out and "medium:1" in out
    assert vox_learn.main(["mine", str(p), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["proposals"] == []
