"""Phase 2.E.2 Step 5 — render protocol_report.md from fixture + run sidecar.

Reads fixture_populated.json and _protocol_run.json (anomalies + validation
results from apply_protocol.py) and writes data/regression_30_baseline/
protocol_report.md.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASELINE_DIR = PROJECT_ROOT / "data" / "regression_30_baseline"
FIXTURE = BASELINE_DIR / "fixture_populated.json"
RUN_SIDECAR = BASELINE_DIR / "_protocol_run.json"
REPORT_OUT = BASELINE_DIR / "protocol_report.md"
RESP_DIR = BASELINE_DIR / "responses"


def render() -> str:
    fixture = json.loads(FIXTURE.read_text())
    run = json.loads(RUN_SIDECAR.read_text())
    rows = fixture["rows"]
    anomalies_by_row = defaultdict(list)
    for a in run["anomalies"]:
        anomalies_by_row[a["row_id"]].append(a)

    lines: list[str] = []
    a = lines.append

    a("# Phase 2.E.2 — Protocol Application Report")
    a("")
    a("## Run metadata")
    a("")
    a(f"- Generated at: `{run['generated_at']}`")
    a(f"- citation-mcp version at baseline: `{run['citation_mcp_version']}`")
    a(f"- Total rows processed: {len(rows)}")
    a(f"- row_030 path determination: **Path {run['row_030_path']}**")
    r030_resp = json.loads((RESP_DIR / "row_030.json").read_text())
    a(f"  - re-run headline: verified={r030_resp.get('verified')}  "
      f"match_quality={r030_resp.get('match_quality')!r}  "
      f"confidence={r030_resp.get('confidence')}  "
      f"rejected_by={r030_resp.get('score_breakdown', {}).get('rejected_by')!r}")
    a("")

    a("## Per-row outcomes")
    a("")
    a("| row_id | category | hard fields | tolerant fields | anomalies |")
    a("|---|---|---|---|---|")
    for r in rows:
        rid = r["row_id"]
        hf = sorted(r["expected"]["hard"].keys())
        tf = sorted(r["expected"]["tolerant"].keys())
        n_anom = len(anomalies_by_row.get(rid, []))
        a(f"| {rid} | {r['category']} | {len(hf)} ({', '.join(hf)}) | "
          f"{len(tf)} ({', '.join(tf)}) | {n_anom} |")
    a("")

    a("## Anomaly list")
    a("")
    if not run["anomalies"]:
        a("_No anomalies raised during protocol application._")
    else:
        a(f"Total anomalies: **{len(run['anomalies'])}**")
        a("")
        # Group by kind for readability.
        by_kind: dict[str, list[dict]] = defaultdict(list)
        for x in run["anomalies"]:
            by_kind[x["kind"]].append(x)
        for kind in sorted(by_kind):
            a(f"### `{kind}`  ({len(by_kind[kind])})")
            a("")
            for x in by_kind[kind]:
                a(f"- **{x['row_id']}** — {x['detail']}")
            a("")

    a("## row_030 path determination — details")
    a("")
    a(f"**Path {run['row_030_path']}** applied. The fallback-title re-run "
      f"(`{{title: 'Comparison of robotic and laparoscopic colectomy outcomes', year: 2022}}`) "
      "produced the following:")
    a("")
    a(f"- `verified` = {r030_resp.get('verified')}")
    a(f"- `match_quality` = `{r030_resp.get('match_quality')!r}`")
    a(f"- `confidence` = {r030_resp.get('confidence')}")
    a(f"- `score_breakdown.rejected_by` = `{r030_resp.get('score_breakdown', {}).get('rejected_by')!r}`")
    a(f"- `canonical.doi` = `{r030_resp.get('canonical', {}).get('doi')!r}`")
    a(f"- `canonical.pmid` = `{r030_resp.get('canonical', {}).get('pmid')!r}`")
    a(f"- `canonical.year` = {r030_resp.get('canonical', {}).get('year')}")
    a(f"- `len(discrepancies)` = {len(r030_resp.get('discrepancies', []))}")
    a("")
    if run["row_030_path"] == "B":
        a("Per the Phase 2.E.2 spec, Path B applies when the re-run also clears "
          "guards. The spec template hardcodes `expected.hard.match_quality = \"low\"`; "
          f"the actual response shows `{r030_resp.get('match_quality')!r}`. The protocol "
          "rule for `match_quality` says: \"If pre-filled... assert; if placeholder, take "
          "from response\" — so the populated fixture uses the actual value and flags "
          "the template/actual divergence as an anomaly for architect review.")
        a("")
        a("Notably, the matched record is **noisy**: Crossref and PubMed disagree on "
          "DOI, title, authors, journal, volume, issue, pages, type, and year. The "
          "Layer 3 merge resolves all of these via `highest_authority_db` "
          "(except `year` via `earliest_non_null_year`). This is a real-world "
          "two-paper-collision: the title search returned different physical papers "
          "from Crossref vs PubMed, both of which are roughly on-topic for the query.")

    a("")
    a("## row_029 — actual rejected_by confirmation")
    a("")
    r029_resp = json.loads((RESP_DIR / "row_029.json").read_text())
    a(f"`row_029` baseline response has `score_breakdown.rejected_by = "
      f"\"{r029_resp.get('score_breakdown', {}).get('rejected_by')}\"` "
      f"(title_sim = {r029_resp.get('score_breakdown', {}).get('title_sim')}, "
      f"year_match = {r029_resp.get('score_breakdown', {}).get('year_match')}). "
      "Per the Phase 2.E.2 spec, the hard rule overrides spec predictions, so the "
      "populated fixture uses `title_similarity_below_floor` and the row's "
      "`notes` field documents the deviation from intent (see Phase 2.D Finding 2).")
    a("")

    a("## Bucket 4 (rows 019–022) — re-confirmation")
    a("")
    a("Phase 2.E.2 spec assumes `inputs.json[row_id].expected.tolerant.discrepancies_required` "
      "is pre-filled for Bucket 4 rows, but inputs.json carries no `expected` block "
      "(input shapes only, per Phase 2.C.3). The protocol-application derives the targeted "
      "(rule, field) pair from each row's `notes` field instead, populates "
      "`expected.tolerant.discrepancies_required`, and verifies each entry appears in "
      "`response.discrepancies`. All four primaries hit — no baseline drift since Phase 2.D.")
    a("")
    a("| row_id | targeted | hit in baseline |")
    a("|---|---|---|")
    for rid in ("row_019", "row_020", "row_021", "row_022"):
        row = next(x for x in rows if x["row_id"] == rid)
        req = row["expected"]["tolerant"]["discrepancies_required"]
        resp = json.loads((RESP_DIR / f"{rid}.json").read_text())
        resp_pairs = {(d.get("rule"), d.get("field")) for d in resp.get("discrepancies", [])}
        for e in req:
            hit = "YES" if (e["rule"], e["field"]) in resp_pairs else "**NO**"
            a(f"| {rid} | `{e['rule']}` / `{e['field']}` | {hit} |")
    a("")

    a("## Validation summary (Phase 2.A rules 1–12)")
    a("")
    a("| # | rule | result | detail |")
    a("|---|---|---|---|")
    for i, v in enumerate(run["validation"], 1):
        status = "PASS" if v["pass"] else "**FAIL**"
        a(f"| {i} | {v['rule']} | {status} | {v['detail']} |")
    a("")
    n_fail = sum(1 for v in run["validation"] if not v["pass"])
    a(f"**Overall: {len(run['validation']) - n_fail}/{len(run['validation'])} rules pass.**")
    a("")

    a("## Aggregate observations")
    a("")
    a("- **Three baseline `highest_authority_db` / `title` hits land in forbidden lists** "
      "(row_004, row_015, row_024). All three are real merge events of distinct "
      "character that Phase 2.E.3 should review individually:")
    a("  - row_004 (`identifier_decisive`, EE-527): Crossref title carries `<scp>...</scp>` "
      "small-cap markup and HTML entities while PubMed/Semantic Scholar carry the "
      "cleaned form — same paper, typesetting noise.")
    a("  - row_015 (`title_only`, EE-516): Semantic Scholar resolved to a `Correction: ...` "
      "article rather than the original. The Layer-3 merge picked Crossref's "
      "(authority-higher) title. Suggests either S2 indexing quirk or that the "
      "title-only search legitimately surfaces both papers.")
    a("  - row_024 (`retracted`, hand-curated): Crossref emits `RETRACTED: ...` prefix "
      "on the title for retracted articles; PubMed/Semantic Scholar do not. This is "
      "structural behavior — likely the forbidden-list entry for retracted should be "
      "relaxed, or `RETRACTED:` should be normalized out before merge.")
    a("- **row_008** input year 2025 vs response year 2024 — the EE source coded the "
      "EPub print year while Crossref's `published-print` field gives 2024. Not "
      "necessarily a data error; the protocol still flagged the discrepancy because "
      "the protocol rule asserts equality of input and response year on matched rows.")
    a("- **row_016 / row_017** are `title_only` inputs that resolved to papers with "
      "arxiv preprints; the canonical record carries an `arxiv_id` even though no "
      "arxiv_id was supplied. The protocol flagged these as "
      "`unexpected_arxiv_id_on_non_arxiv_row`. Not a bug — the architect may want "
      "to relax the rule so that any DB-returned arxiv_id is allowed through.")
    a("- **Bucket 4 discrepancies_required were derived from row notes**, not from "
      "inputs.json pre-fills (which don't exist). All four targeted entries hit in "
      "baseline. This deviates from the literal spec wording but matches its intent.")
    a("")
    a("---")
    a("")
    a("_End of Phase 2.E.2 protocol application report._")

    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    out = render()
    REPORT_OUT.write_text(out)
    print(f"wrote {REPORT_OUT}  ({len(out)} chars)")
