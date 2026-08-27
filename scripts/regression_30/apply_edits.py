"""Phase 2.F Step 1 — apply Phase 2.E.3's 13 edits to fixture_populated.json.

Reads data/regression_30_baseline/fixture_populated.json, applies edits keyed
by row_id via a small operation set (remove_forbidden_entry, set_hard_field,
append_note, replace_notes), and writes fixture_edited.json.

Each operation fails defensively if its precondition isn't met (e.g.
remove_forbidden_entry raises if the entry isn't present), so this script
also confirms baseline state matches Phase 2.E.3's read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASELINE_DIR = PROJECT_ROOT / "data" / "regression_30_baseline"
SRC = BASELINE_DIR / "fixture_populated.json"
DST = BASELINE_DIR / "fixture_edited.json"


EDITS: list[dict[str, Any]] = [
    {"row_id": "row_004", "op": "remove_forbidden_entry",
     "value": {"rule": "highest_authority_db", "field": "title"}},
    {"row_id": "row_004", "op": "append_note",
     "value": "Title discrepancy on Crossref <scp> SGML markup is baseline behavior; not a regression."},

    {"row_id": "row_008", "op": "set_hard_field", "field": "year", "value": 2024},
    {"row_id": "row_008", "op": "append_note",
     "value": "EE stored OpenAlex e-pub year 2025; Crossref published-print 2024. Canonical year resolves to 2024 (earliest non-null); year guard threshold (>1) not triggered."},

    {"row_id": "row_015", "op": "remove_forbidden_entry",
     "value": {"rule": "highest_authority_db", "field": "title"}},
    {"row_id": "row_015", "op": "append_note",
     "value": "S2 returned correction article for title-only search; merge selected Crossref's authoritative original. Baseline behavior for title-only search where corrections exist."},

    {"row_id": "row_016", "op": "set_hard_field", "field": "arxiv_id_resolved", "value": "2409.14287"},
    {"row_id": "row_016", "op": "append_note",
     "value": "Resolved with arxiv_id from S2 preprint metadata; canonical record includes all identifiers DBs surface."},

    {"row_id": "row_017", "op": "set_hard_field", "field": "arxiv_id_resolved", "value": "2505.10251"},
    {"row_id": "row_017", "op": "append_note",
     "value": "Resolved with arxiv_id from S2 preprint metadata; canonical record includes all identifiers DBs surface."},

    {"row_id": "row_024", "op": "remove_forbidden_entry",
     "value": {"rule": "highest_authority_db", "field": "title"}},
    {"row_id": "row_024", "op": "append_note",
     "value": "Crossref RETRACTED: title prefix vs PubMed/S2 cleaned title is structural for retracted papers; baseline behavior pending v1.1 checkRetraction tool."},

    {"row_id": "row_030", "op": "replace_notes",
     "value": ("Both the original ('Surgical robotics comparative review') and "
               "fallback ('Comparison of robotic and laparoscopic colectomy "
               "outcomes') adversarial titles returned medium-confidence "
               "matches with substantial inter-DB discrepancies (9 in "
               "baseline; Crossref and PubMed resolved to different physical "
               "papers, both on-topic for the deliberately generic title). "
               "Category retained as 'adversarial' since original intent was "
               "to test rejection paths; row now exercises noisy-match-with-"
               "multiple-discrepancies regression coverage. "
               "requires_review=false (medium quality, not low).")},
]


def find_row(rows: list[dict], rid: str) -> dict:
    for r in rows:
        if r["row_id"] == rid:
            return r
    raise SystemExit(f"row not found: {rid}")


def apply_edit(rows: list[dict], edit: dict) -> None:
    rid = edit["row_id"]
    op = edit["op"]
    row = find_row(rows, rid)

    if op == "remove_forbidden_entry":
        target = edit["value"]
        forbidden = row["expected"]["tolerant"]["discrepancies_forbidden"]
        for i, e in enumerate(forbidden):
            if e.get("rule") == target["rule"] and e.get("field") == target["field"]:
                forbidden.pop(i)
                return
        raise SystemExit(
            f"remove_forbidden_entry on {rid}: entry {target!r} not found in "
            f"discrepancies_forbidden={forbidden!r}"
        )

    if op == "set_hard_field":
        field = edit["field"]
        if field not in row["expected"]["hard"]:
            raise SystemExit(
                f"set_hard_field on {rid}: field {field!r} not present in expected.hard"
            )
        row["expected"]["hard"][field] = edit["value"]
        return

    if op == "append_note":
        existing = row.get("notes", "") or ""
        sep = " | " if existing else ""
        row["notes"] = f"{existing}{sep}{edit['value']}"
        return

    if op == "replace_notes":
        row["notes"] = edit["value"]
        return

    raise SystemExit(f"unknown op: {op!r}")


def main() -> None:
    fixture = json.loads(SRC.read_text())
    rows = fixture["rows"]
    for edit in EDITS:
        apply_edit(rows, edit)
    DST.write_text(json.dumps(fixture, indent=2, ensure_ascii=False))
    print(f"wrote {DST}  edits_applied={len(EDITS)}")


if __name__ == "__main__":
    main()
