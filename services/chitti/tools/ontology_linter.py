"""Deterministic review-time ontology checks; deliberately not imported by ``app``."""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MechanicalViolation:
    passage_id: str
    value: str
    location: str


def derive_forbidden_values(cases_path: Path) -> set[str]:
    """Derive case ids, oracle figures and named oracle values from cases.yaml."""
    import yaml

    cases = yaml.safe_load(cases_path.read_text()) or {}
    values: set[str] = set()

    def visit(value: object, key: str = "", result_value: bool = False) -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                child_key = str(child_key)
                visit(
                    child,
                    child_key,
                    result_value or child_key in {"expected_facts", "expected_rows"}
                    and child_key not in {"field", "resource", "resources"},
                )
            return
        if isinstance(value, list):
            for child in value:
                visit(child, key, result_value)
            return
        if isinstance(value, int | float) and not isinstance(value, bool):
            if key in {"minimum_phase", "schema_version"}:
                return
            values.add(str(value))
            return
        if not isinstance(value, str):
            return
        text = value.strip()
        if re.fullmatch(r"Q\d+", text, re.I):
            values.add(text)
        if (
            result_value
            and len(text) >= 3
            and not re.search(r"[.!?]", text)
            and not re.fullmatch(r"[a-z][a-z0-9_]*", text)
        ):
            values.add(text)
        if re.search(r"\d", text) and key not in {"question", "source_text", "caveats"}:
            values.add(text)

    visit(cases)
    return values


def register_governed_values(repo_root: Path) -> set[str]:
    """Read the Register's authored vocabularies without importing its application."""
    values: set[str] = set()
    refdata = repo_root / "services" / "register" / "app" / "seed" / "refdata.py"
    enums = repo_root / "services" / "register" / "app" / "core" / "enums.py"
    lifecycle = repo_root / "packages" / "evam-backend-core" / "evam_backend_core" / "lifecycle.py"
    tree = ast.parse(refdata.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "REF_VALUES" for target in node.targets
        ):
            raw = ast.literal_eval(node.value)
            values.update(item for group in raw.values() for item in group)
    tree = ast.parse(enums.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                continue
            if isinstance(value, str):
                values.add(value)
    tree = ast.parse(lifecycle.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AnnAssign):
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            name = target.id if isinstance(target, ast.Name) else ""
            if name.endswith(("_STATUSES", "_STAGES")) or name == "DEAL_FUNNEL_STAGES":
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    continue
                values.update(item for item in value if isinstance(item, str))
    return values


def mechanical_scan(
    ontology: dict,
    *,
    forbidden_values: set[str] = frozenset(),
    prompt_text: str = "",
    governed_values: set[str] | None = None,
) -> list[MechanicalViolation]:
    """Find case identifiers, known evaluation values and ungoverned literals."""
    texts = {
        passage["id"]: (str(passage.get("content") or ""), dict(passage.get("metadata") or {}))
        for passage in ontology.get("passages", [])
    }
    if prompt_text:
        texts["<prompt_constants>"] = (prompt_text, {})
    allowed = {str(value).casefold() for value in (governed_values or set())}
    violations: list[MechanicalViolation] = []
    for passage_id, (text, metadata) in texts.items():
        for value in sorted(forbidden_values):
            if (
                not metadata.get("vocabulary")
                and value.casefold() not in allowed
                and _contains_unmanaged_value(text, value, allowed)
            ):
                violations.append(MechanicalViolation(passage_id, value, "forbidden_value"))
        numeric_literal = (
            r"\bQ\d{2,}\b|\b(?:₹|Rs\.?\s*)?"
            r"[0-9]{1,3}(?:,[0-9]{3})+(?:\.\d+)?\b"
        )
        for match in re.finditer(numeric_literal, text, re.I):
            violations.append(MechanicalViolation(passage_id, match.group(0), "evaluation_literal"))
    return violations


def _contains_unmanaged_value(text: str, value: str, governed_values: set[str]) -> bool:
    for match in re.finditer(re.escape(value), text, re.IGNORECASE):
        covered = False
        for governed in governed_values:
            if governed.casefold() == value.casefold():
                continue
            for governed_match in re.finditer(re.escape(governed), text, re.IGNORECASE):
                if governed_match.start() <= match.start() and governed_match.end() >= match.end():
                    covered = True
                    break
            if covered:
                break
        if not covered:
            return True
    return False


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ontology", type=Path)
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--prompt-file", type=Path)
    args = parser.parse_args()
    ontology = json.loads(args.ontology.read_text())
    forbidden = derive_forbidden_values(args.cases) if args.cases else set()
    prompt_text = args.prompt_file.read_text() if args.prompt_file else ""
    governed = register_governed_values(Path(__file__).resolve().parents[3])
    violations = mechanical_scan(
        ontology,
        forbidden_values=forbidden,
        prompt_text=prompt_text,
        governed_values=governed,
    )
    output = {"mechanical_violations": [item.__dict__ for item in violations]}
    print(json.dumps(output, indent=2))
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
