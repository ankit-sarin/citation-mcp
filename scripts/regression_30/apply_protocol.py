"""Phase 2.E.2 Step 2 + 3 + 4 — apply field-extraction protocol.

Reads inputs.json + responses/row_NNN.json for all 30 primary rows,
builds fixture_populated.json per the Phase 2.E.2 protocol, and runs
Phase 2.A's 12 validation rules.

Anomalies and validation results are returned for use in the protocol
report (see build_report.py).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from citation_mcp import __version__ as CITATION_MCP_VERSION
from citation_mcp.scoring import (
    normalize_doi,
    _normalize_arxiv,
    _normalize_pmid,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASELINE_DIR = PROJECT_ROOT / "data" / "regression_30_baseline"
INPUTS_FILE = BASELINE_DIR / "inputs.json"
RESP_DIR = BASELINE_DIR / "responses"
FIXTURE_OUT = BASELINE_DIR / "fixture_populated.json"


# Category → forbidden discrepancy entries (per Phase 2.E.2 spec).
FORBIDDEN_BY_CATEGORY: dict[str, list[dict[str, str]]] = {
    "identifier_decisive": [
        {"rule": "highest_authority_db", "field": "doi"},
        {"rule": "highest_authority_db", "field": "title"},
        {"rule": "earliest_non_null_year", "field": "year"},
    ],
    "retracted": [
        {"rule": "highest_authority_db", "field": "doi"},
        {"rule": "highest_authority_db", "field": "title"},
        {"rule": "earliest_non_null_year", "field": "year"},
    ],
    "corrigendum": [
        {"rule": "highest_authority_db", "field": "doi"},
        {"rule": "highest_authority_db", "field": "title"},
        {"rule": "earliest_non_null_year", "field": "year"},
    ],
    "pubmed_native_edge": [
        {"rule": "highest_authority_db", "field": "doi"},
        {"rule": "highest_authority_db", "field": "title"},
        {"rule": "earliest_non_null_year", "field": "year"},
    ],
    "bibliographic_only": [
        {"rule": "highest_authority_db", "field": "title"},
        {"rule": "earliest_non_null_year", "field": "year"},
    ],
    "title_only": [
        {"rule": "highest_authority_db", "field": "title"},
    ],
    "known_discrepancy": [
        {"rule": "highest_authority_db", "field": "doi"},
        {"rule": "earliest_non_null_year", "field": "year"},
    ],
    "arxiv_opt_in": [
        {"rule": "highest_authority_db", "field": "title"},
        {"rule": "earliest_non_null_year", "field": "year"},
    ],
    "adversarial": [],
}


# Bucket 4 (known_discrepancy) target rules derived from row notes
# (since inputs.json has no pre-filled expected.tolerant.discrepancies_required).
BUCKET4_REQUIRED: dict[str, list[dict[str, str]]] = {
    "row_019": [{"rule": "highest_authority_db", "field": "authors"}],
    "row_020": [{"rule": "highest_authority_db", "field": "authors"}],
    "row_021": [{"rule": "highest_authority_db", "field": "type"}],
    "row_022": [{"rule": "highest_authority_db", "field": "journal"}],
}


def load_response(rid: str) -> dict[str, Any]:
    return json.loads((RESP_DIR / f"{rid}.json").read_text())


def pick_citation_count(canonical_cc: Any) -> dict[str, Any] | None:
    """Build expected.tolerant.citation_count entry, or None to omit."""
    if not isinstance(canonical_cc, dict):
        return None
    keys = [k for k in ("openalex", "semantic_scholar") if k in canonical_cc and canonical_cc[k] is not None]
    if not keys:
        return None
    value = max(canonical_cc[k] for k in keys)
    return {"value": value, "tolerance_pct": 20}


def populate_matched_row(input_row: dict, resp: dict, anomalies: list[dict]) -> dict:
    rid = input_row["row_id"]
    inp = input_row["input"]
    canonical = resp.get("canonical") or {}

    hard: dict[str, Any] = {}
    hard["match_found"] = bool(resp.get("verified"))
    if not hard["match_found"]:
        anomalies.append({
            "row_id": rid,
            "kind": "matched_row_unexpectedly_not_verified",
            "detail": "non-adversarial row has verified=false",
        })

    # DOI
    resp_doi = canonical.get("doi")
    hard["doi_resolved"] = resp_doi
    if inp.get("doi") is not None and resp_doi is not None:
        if normalize_doi(inp["doi"]) != resp_doi:
            anomalies.append({
                "row_id": rid,
                "kind": "doi_mismatch",
                "detail": f"input.doi={inp['doi']!r} normalized={normalize_doi(inp['doi'])!r} response.doi={resp_doi!r}",
            })

    # PMID
    resp_pmid = canonical.get("pmid")
    hard["pmid_resolved"] = resp_pmid
    if inp.get("pmid") is not None and resp_pmid is not None:
        if _normalize_pmid(inp["pmid"]) != resp_pmid:
            anomalies.append({
                "row_id": rid,
                "kind": "pmid_mismatch",
                "detail": f"input.pmid={inp['pmid']!r} normalized={_normalize_pmid(inp['pmid'])!r} response.pmid={resp_pmid!r}",
            })

    # arXiv
    resp_arxiv = canonical.get("arxiv_id")
    hard["arxiv_id_resolved"] = resp_arxiv
    if inp.get("arxiv_id") is not None:
        if resp_arxiv is None or _normalize_arxiv(inp["arxiv_id"]) != _normalize_arxiv(resp_arxiv):
            anomalies.append({
                "row_id": rid,
                "kind": "arxiv_mismatch",
                "detail": f"input.arxiv_id={inp['arxiv_id']!r} response.arxiv_id={resp_arxiv!r}",
            })
    else:
        # arxiv_id_resolved should be null on non-arXiv rows.
        if resp_arxiv is not None:
            anomalies.append({
                "row_id": rid,
                "kind": "unexpected_arxiv_id_on_non_arxiv_row",
                "detail": f"response.arxiv_id={resp_arxiv!r}",
            })

    # match_quality & confidence — no pre-fills in inputs.json, take from response.
    hard["match_quality"] = resp.get("match_quality")
    hard["confidence"] = resp.get("confidence")

    # first_author_surname
    authors = canonical.get("authors") or []
    if not authors:
        anomalies.append({
            "row_id": rid,
            "kind": "empty_canonical_authors",
            "detail": "canonical.authors missing or empty",
        })
        hard["first_author_surname"] = None
    else:
        hard["first_author_surname"] = authors[0].get("family")

    # year
    resp_year = canonical.get("year")
    hard["year"] = resp_year
    if inp.get("year") is not None and resp_year is not None:
        if int(inp["year"]) != int(resp_year):
            anomalies.append({
                "row_id": rid,
                "kind": "year_mismatch",
                "detail": f"input.year={inp['year']} response.year={resp_year}",
            })

    return hard


def populate_adversarial_row(input_row: dict, resp: dict, anomalies: list[dict]) -> dict:
    """Adversarial-shaped (Path A or row_029): match_found=false + rejected_by."""
    rid = input_row["row_id"]
    hard: dict[str, Any] = {}
    hard["match_found"] = bool(resp.get("verified"))
    if hard["match_found"]:
        anomalies.append({
            "row_id": rid,
            "kind": "adversarial_row_unexpectedly_verified",
            "detail": "adversarial row has verified=true (Path-A handling expected verified=false)",
        })
    hard["rejected_by"] = resp.get("score_breakdown", {}).get("rejected_by")
    return hard


def build_row(input_row: dict, anomalies: list[dict], row_030_path: str) -> dict:
    rid = input_row["row_id"]
    category = input_row["category"]
    source = input_row["source"]
    notes = input_row.get("notes", "")
    inp = input_row["input"]
    resp = load_response(rid)
    canonical = resp.get("canonical") or {}

    # Hard expected.
    if rid == "row_029":
        hard = populate_adversarial_row(input_row, resp, anomalies)
        new_notes = (
            "Originally intended to exercise year_off_by_more_than_one guard; "
            "actually exercises title_similarity_below_floor because title-only "
            "inputs with null-year DB candidates skip the year guard and fall "
            "through to title-sim. See Phase 2.D Finding 2."
        )
    elif rid == "row_030":
        if row_030_path == "A":
            hard = populate_adversarial_row(input_row, resp, anomalies)
            rejected = hard["rejected_by"]
            new_notes = (
                "Original adversarial title 'Surgical robotics comparative review' "
                "returned a low-quality match (distractor passed guards). "
                "Fallback title 'Comparison of robotic and laparoscopic colectomy "
                f"outcomes' used; guard fired as {rejected}."
            )
        else:  # Path B
            authors = canonical.get("authors") or []
            first_author = authors[0].get("family") if authors else None
            if not authors:
                anomalies.append({
                    "row_id": rid,
                    "kind": "empty_canonical_authors",
                    "detail": "Path-B row_030 has no canonical.authors",
                })
            actual_mq = resp.get("match_quality")
            hard = {
                "match_found": True,
                "match_quality": actual_mq,
                "confidence": resp.get("confidence"),
                "doi_resolved": canonical.get("doi"),
                "pmid_resolved": canonical.get("pmid"),
                "arxiv_id_resolved": None,
                "first_author_surname": first_author,
                "year": canonical.get("year"),
            }
            new_notes = (
                "Both original and fallback adversarial titles cleared guards as "
                f"low-quality matches (actual match_quality={actual_mq!r}; "
                "Phase 2.E.2 spec assumed 'low'). Row exercises the non-rejection "
                "path rather than a guard rejection. Category retained as "
                "'adversarial' since intent was to test rejection-class behavior."
            )
            if actual_mq != "low":
                anomalies.append({
                    "row_id": rid,
                    "kind": "row_030_path_b_quality_above_low",
                    "detail": f"Path-B template hardcodes match_quality='low' but actual={actual_mq!r}; used actual value.",
                })
    elif category == "adversarial":
        # Defensive — shouldn't happen given current inputs.
        hard = populate_adversarial_row(input_row, resp, anomalies)
        new_notes = notes
    else:
        hard = populate_matched_row(input_row, resp, anomalies)
        new_notes = notes

    # Tolerant block.
    tolerant: dict[str, Any] = {}
    cc_entry = pick_citation_count(canonical.get("citation_count"))
    if cc_entry is not None:
        tolerant["citation_count"] = cc_entry

    # Discrepancies required: Bucket 4 derived from notes; others empty.
    if rid in BUCKET4_REQUIRED:
        required = list(BUCKET4_REQUIRED[rid])
        # Verify each required entry shows up in response.discrepancies.
        resp_disc = resp.get("discrepancies", []) or []
        resp_pairs = {(d.get("rule"), d.get("field")) for d in resp_disc}
        for entry in required:
            if (entry["rule"], entry["field"]) not in resp_pairs:
                anomalies.append({
                    "row_id": rid,
                    "kind": "bucket4_required_discrepancy_missing",
                    "detail": f"required {entry!r} not found in response.discrepancies",
                })
    else:
        required = []
    tolerant["discrepancies_required"] = required

    # Discrepancies forbidden: category-driven; flag any baseline hits.
    forbidden = [dict(e) for e in FORBIDDEN_BY_CATEGORY.get(category, [])]
    resp_disc = resp.get("discrepancies", []) or []
    resp_pairs = {(d.get("rule"), d.get("field")) for d in resp_disc}
    for entry in forbidden:
        if (entry["rule"], entry["field"]) in resp_pairs:
            anomalies.append({
                "row_id": rid,
                "kind": "baseline_forbidden_hit",
                "detail": (
                    f"forbidden {entry!r} actually appears in baseline response.discrepancies — "
                    f"Phase 2.E.3 should decide whether to remove from forbidden list."
                ),
            })
    tolerant["discrepancies_forbidden"] = forbidden

    # allowed_soft_failures: arXiv rows get ["arxiv"], else [].
    is_arxiv_row = inp.get("arxiv_id") is not None or category == "arxiv_opt_in"
    tolerant["allowed_soft_failures"] = ["arxiv"] if is_arxiv_row else []

    # Snapshot = raw response.
    snapshot = resp

    row: dict[str, Any] = {
        "row_id": rid,
        "category": category,
        "source": source,
        "notes": new_notes,
        "input": inp,
        "expected": {
            "hard": hard,
            "tolerant": tolerant,
            "snapshot": snapshot,
        },
    }
    return row


def determine_row_030_path() -> str:
    resp = load_response("row_030")
    return "A" if not resp.get("verified") else "B"


def build_fixture() -> tuple[dict, list[dict], str]:
    inputs = json.loads(INPUTS_FILE.read_text())
    if len(inputs) != 30:
        raise SystemExit(f"expected 30 primary rows, got {len(inputs)}")

    row_030_path = determine_row_030_path()
    anomalies: list[dict] = []
    rows = [build_row(r, anomalies, row_030_path) for r in inputs]
    fixture = {
        "fixture_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "citation_mcp_version_at_baseline": CITATION_MCP_VERSION,
        "tolerance_defaults": {
            "citation_count_pct": 20,
            "confidence_abs": 0.05,
        },
        "rows": rows,
    }
    return fixture, anomalies, row_030_path


# ---------------------------------------------------------------------------
# Phase 2.A validation rules
# ---------------------------------------------------------------------------


VALID_DISCREPANCY_RULES = {"highest_authority_db", "earliest_non_null_year", "report_both_diff_gt_20pct"}

EXPECTED_CATEGORY_COUNTS = {
    "identifier_decisive": 8,
    "bibliographic_only": 6,
    "title_only": 4,
    "known_discrepancy": 4,
    "pubmed_native_edge": 1,
    "retracted": 2,
    "corrigendum": 1,
    "arxiv_opt_in": 2,
    "adversarial": 2,
}

ADVERSARIAL_REJECTED_BY_ROW_029 = {
    "year_off_by_more_than_one",
    "first_author_mismatch_low_title_sim",
    "title_similarity_below_floor",
}


def validate(fixture: dict, row_030_path: str) -> list[dict]:
    """Return a list of {rule, pass, detail} dicts (Phase 2.A rules 1–12)."""
    rows = fixture["rows"]
    results: list[dict] = []

    # Rule 1: exactly 30 rows.
    ok = len(rows) == 30
    results.append({
        "rule": "1: exactly 30 rows",
        "pass": ok,
        "detail": f"count={len(rows)}",
    })

    # Rule 2: row_001..row_030 unique + contiguous.
    expected_ids = [f"row_{i:03d}" for i in range(1, 31)]
    actual_ids = [r["row_id"] for r in rows]
    ok = actual_ids == expected_ids
    results.append({
        "rule": "2: row_001..row_030 unique + contiguous in order",
        "pass": ok,
        "detail": "ok" if ok else f"actual={actual_ids}",
    })

    # Rule 3: category counts.
    from collections import Counter
    actual_counts = Counter(r["category"] for r in rows)
    ok = dict(actual_counts) == EXPECTED_CATEGORY_COUNTS
    results.append({
        "rule": "3: category counts match expected distribution",
        "pass": ok,
        "detail": "ok" if ok else f"actual={dict(actual_counts)} expected={EXPECTED_CATEGORY_COUNTS}",
    })

    # Rule 4 — skipped per spec.
    results.append({
        "rule": "4: (skipped — provenance-only, verified Phase 2.C.2)",
        "pass": True,
        "detail": "skipped",
    })

    # Rule 5: source value validity.
    hand_curated_ids = {"row_024", "row_025", "row_027", "row_028", "row_029", "row_030"}
    ee_pat = re.compile(r"^EE-\d+$")
    rule5_fails = []
    for r in rows:
        rid = r["row_id"]
        src = r["source"]
        if rid in hand_curated_ids:
            if src != "hand_curated":
                rule5_fails.append(f"{rid}: source={src!r} expected 'hand_curated'")
        else:
            if not ee_pat.match(src):
                rule5_fails.append(f"{rid}: source={src!r} doesn't match EE-NNN")
    results.append({
        "rule": "5: hand-curated rows have source='hand_curated'; others match ^EE-\\d+$",
        "pass": not rule5_fails,
        "detail": "ok" if not rule5_fails else "; ".join(rule5_fails),
    })

    # Rule 6: adversarial row constraints.
    rule6_fails = []
    r029 = next(r for r in rows if r["row_id"] == "row_029")
    if r029["expected"]["hard"].get("match_found") is not False:
        rule6_fails.append("row_029: match_found != false")
    if r029["expected"]["hard"].get("rejected_by") not in ADVERSARIAL_REJECTED_BY_ROW_029:
        rule6_fails.append(f"row_029: rejected_by={r029['expected']['hard'].get('rejected_by')!r} not in expected set")
    r030 = next(r for r in rows if r["row_id"] == "row_030")
    if row_030_path == "A":
        if r030["expected"]["hard"].get("match_found") is not False:
            rule6_fails.append("row_030 Path A: match_found != false")
    else:
        if r030["expected"]["hard"].get("match_found") is not True:
            rule6_fails.append("row_030 Path B: match_found != true")
    for arow in (r029, r030):
        inp = arow["input"]
        if not any(inp.get(k) for k in ("doi", "pmid", "title")):
            rule6_fails.append(f"{arow['row_id']}: missing all of doi/pmid/title")
    results.append({
        "rule": "6: adversarial row constraints (row_029 rejected; row_030 Path A/B)",
        "pass": not rule6_fails,
        "detail": "ok" if not rule6_fails else "; ".join(rule6_fails),
    })

    # Rule 7: non-adversarial rows have match_found=true + populated first_author_surname/year/match_quality.
    rule7_fails = []
    for r in rows:
        if r["category"] == "adversarial":
            continue
        h = r["expected"]["hard"]
        if h.get("match_found") is not True:
            rule7_fails.append(f"{r['row_id']}: match_found != true")
        if not h.get("first_author_surname"):
            rule7_fails.append(f"{r['row_id']}: first_author_surname empty")
        if h.get("year") is None:
            rule7_fails.append(f"{r['row_id']}: year is null")
        if h.get("match_quality") is None:
            rule7_fails.append(f"{r['row_id']}: match_quality is null")
    results.append({
        "rule": "7: non-adversarial rows have populated hard fields",
        "pass": not rule7_fails,
        "detail": "ok" if not rule7_fails else "; ".join(rule7_fails),
    })

    # Rule 8: arXiv rows.
    rule8_fails = []
    for rid in ("row_027", "row_028"):
        r = next(x for x in rows if x["row_id"] == rid)
        inp = r["input"]
        if not inp.get("arxiv_id"):
            rule8_fails.append(f"{rid}: missing input.arxiv_id")
        if not any(inp.get(k) for k in ("doi", "pmid", "title")):
            rule8_fails.append(f"{rid}: missing all of doi/pmid/title")
        if "arxiv" not in r["expected"]["tolerant"].get("allowed_soft_failures", []):
            rule8_fails.append(f"{rid}: allowed_soft_failures missing 'arxiv'")
    results.append({
        "rule": "8: arXiv rows have arxiv_id + ≥1 biblio field + allowed_soft_failures=['arxiv']",
        "pass": not rule8_fails,
        "detail": "ok" if not rule8_fails else "; ".join(rule8_fails),
    })

    # Rule 9: title-only rows (015–018) match_quality == "medium".
    rule9_fails = []
    for rid in ("row_015", "row_016", "row_017", "row_018"):
        r = next(x for x in rows if x["row_id"] == rid)
        mq = r["expected"]["hard"].get("match_quality")
        if mq != "medium":
            rule9_fails.append(f"{rid}: match_quality={mq!r}")
    results.append({
        "rule": "9: title-only rows have match_quality='medium'",
        "pass": not rule9_fails,
        "detail": "ok" if not rule9_fails else "; ".join(rule9_fails),
    })

    # Rule 10: no row has expected.snapshot == null.
    rule10_fails = [r["row_id"] for r in rows if r["expected"].get("snapshot") is None]
    results.append({
        "rule": "10: no row has expected.snapshot=null",
        "pass": not rule10_fails,
        "detail": "ok" if not rule10_fails else f"null on: {rule10_fails}",
    })

    # Rule 11: allowed_soft_failures values subset of {"arxiv"}.
    rule11_fails = []
    for r in rows:
        vals = r["expected"]["tolerant"].get("allowed_soft_failures", [])
        bad = [v for v in vals if v not in {"arxiv"}]
        if bad:
            rule11_fails.append(f"{r['row_id']}: bad values {bad}")
    results.append({
        "rule": "11: allowed_soft_failures ⊆ {'arxiv'}",
        "pass": not rule11_fails,
        "detail": "ok" if not rule11_fails else "; ".join(rule11_fails),
    })

    # Rule 12: discrepancy entries have valid rule + non-empty field.
    rule12_fails = []
    for r in rows:
        for kind in ("discrepancies_required", "discrepancies_forbidden"):
            for entry in r["expected"]["tolerant"].get(kind, []):
                if entry.get("rule") not in VALID_DISCREPANCY_RULES:
                    rule12_fails.append(f"{r['row_id']}.{kind}: bad rule={entry.get('rule')!r}")
                f = entry.get("field")
                if not isinstance(f, str) or not f.strip():
                    rule12_fails.append(f"{r['row_id']}.{kind}: bad field={entry.get('field')!r}")
    results.append({
        "rule": "12: discrepancy entries have valid rule + non-empty field",
        "pass": not rule12_fails,
        "detail": "ok" if not rule12_fails else "; ".join(rule12_fails),
    })

    return results


if __name__ == "__main__":
    fixture, anomalies, row_030_path = build_fixture()
    FIXTURE_OUT.write_text(json.dumps(fixture, indent=2, ensure_ascii=False))
    print(f"wrote {FIXTURE_OUT}  rows={len(fixture['rows'])}  anomalies={len(anomalies)}  row_030_path={row_030_path}")
    val = validate(fixture, row_030_path)
    n_fail = sum(1 for v in val if not v["pass"])
    print(f"validation: {len(val) - n_fail}/{len(val)} pass, {n_fail} fail")
    # Persist anomalies + validation to a JSON sidecar for the report builder.
    (BASELINE_DIR / "_protocol_run.json").write_text(json.dumps({
        "row_030_path": row_030_path,
        "anomalies": anomalies,
        "validation": val,
        "citation_mcp_version": CITATION_MCP_VERSION,
        "generated_at": fixture["generated_at"],
    }, indent=2))
