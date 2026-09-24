import importlib.util
import sys
from pathlib import Path

_LINTER_PATH = Path(__file__).resolve().parents[1] / "tools" / "ontology_linter.py"
_LINTER_SPEC = importlib.util.spec_from_file_location("ontology_linter_review", _LINTER_PATH)
assert _LINTER_SPEC and _LINTER_SPEC.loader
_LINTER = importlib.util.module_from_spec(_LINTER_SPEC)
sys.modules[_LINTER_SPEC.name] = _LINTER
_LINTER_SPEC.loader.exec_module(_LINTER)

derive_forbidden_values = _LINTER.derive_forbidden_values
mechanical_scan = _LINTER.mechanical_scan
register_governed_values = _LINTER.register_governed_values


def test_mechanical_scan_uses_case_derived_values_and_prompt_text(tmp_path: Path):
    cases = tmp_path / "cases.yaml"
    cases.write_text(
        "cases:\n"
        "  - id: Q99\n"
        "    expected_facts:\n"
        "      company: Oracle Example\n"
        "      amount: '1,234.50'\n"
        "      ungoverned_value: Stored Surprise\n"
    )
    forbidden = derive_forbidden_values(cases)
    assert {"Q99", "Oracle Example", "1,234.50", "Stored Surprise"} <= forbidden
    ontology = {"passages": [{"id": "p", "content": "Oracle Example"}]}
    violations = mechanical_scan(
        ontology,
        forbidden_values=forbidden,
        prompt_text="Q99 is not runtime ontology.",
    )
    assert {(item.passage_id, item.value) for item in violations} == {
        ("p", "Oracle Example"),
        ("<prompt_constants>", "Q99"),
    }


def test_mechanical_scan_detects_workbook_figures_and_ungoverned_values():
    ontology = {
        "passages": [
            {"id": "figure", "content": "The expected total is ₹1,234.50."},
            {"id": "stored", "content": "The stored stage is Stored Surprise."},
        ]
    }

    violations = mechanical_scan(
        ontology,
        forbidden_values={"1,234.50", "Stored Surprise"},
    )

    assert any(
        item.passage_id == "figure" and item.location == "evaluation_literal"
        for item in violations
    )
    assert any(
        item.passage_id == "stored"
        and item.value == "Stored Surprise"
        and item.location == "forbidden_value"
        for item in violations
    )


def test_each_forbidden_category_is_scanned_in_ontology_and_prompts():
    samples = ("Q99", "Oracle Example", "Stored Surprise", "₹1,234.50")

    for sample in samples:
        violations = mechanical_scan(
            {"passages": [{"id": "ontology", "content": sample}]},
            forbidden_values={sample.removeprefix("₹")},
            prompt_text=sample,
        )

        assert {item.passage_id for item in violations} == {
            "ontology",
            "<prompt_constants>",
        }


def test_governed_register_value_is_not_forbidden_only_because_oracle_uses_it():
    ontology = {"passages": [{"id": "stage", "content": "The stage is Sanctioned."}]}

    violations = mechanical_scan(
        ontology,
        forbidden_values={"Sanctioned"},
        governed_values={"Sanctioned"},
    )

    assert violations == []


def test_register_governed_values_use_canonical_lending_stage():
    repo_root = Path(__file__).resolve().parents[3]

    governed = register_governed_values(repo_root)

    assert "CP/CS Completed" in governed
    assert "Documentation" not in governed
