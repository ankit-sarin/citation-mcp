"""Phase GATE-OPT1 — uniform hard-tier demotion of regression_30.json.

Rewrites every row's expected.hard so the live gate asserts ONLY
carrier-invariant properties:

  * Matched rows (match_found True): every field in the old hard block is a
    positive upstream-echoed value/decision (year, doi/pmid/arxiv resolved,
    first_author_surname, match_quality, confidence, match_found=True) -> ALL
    demote to the non-gating snapshot tier; the new hard block is empty {}.
  * No-match / fabrication rows (match_found False): keep the carrier-invariant
    negative — match_found=False plus null doi/pmid/arxiv (a real DB matching a
    fabrication, or surfacing an identifier for one, is always a connector bug,
    never an outage artifact). rejected_by demotes to snapshot (its exact value
    is carrier-variant — see Phase 2.D Finding 2).

The snapshot tier already carries every demoted value (full raw response), so
drift still surfaces non-gating. Run once; idempotent.
"""

import json
from pathlib import Path

P = Path(__file__).resolve().parents[2] / "tests/fixtures/regression_30.json"


def main() -> None:
    data = json.loads(P.read_text(encoding="utf-8"))
    for r in data["rows"]:
        hard = r["expected"]["hard"]
        if hard.get("match_found") is False:
            r["expected"]["hard"] = {
                "match_found": False,
                "doi_resolved": None,
                "pmid_resolved": None,
                "arxiv_id_resolved": None,
            }
        else:
            r["expected"]["hard"] = {}
    P.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Demoted hard blocks for {len(data['rows'])} rows -> {P}")


if __name__ == "__main__":
    main()
