"""Match-quality scoring + canonical record merging.

Layers:
  Layer 1 — identifier-decisive (DOI, PMID, arXiv, OpenAlex, Semantic Scholar).
  Layer 2 — weighted-field bibliographic score with sanity guards.
  Layer 3 — merge_canonical_records: reconcile per-field across DBs.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from rapidfuzz import fuzz

# ---------------------------------------------------------------------------
# Journal abbreviation lookup (ISO 4 ↔ full name)
# Pragmatic curated set for biomedical/surgical journals — extend as needed.
# ---------------------------------------------------------------------------

_JOURNAL_ABBREV_TO_FULL: dict[str, str] = {
    "n engl j med": "new england journal of medicine",
    "nejm": "new england journal of medicine",
    "jama": "jama",
    "jama netw open": "jama network open",
    "jama surg": "jama surgery",
    "jama intern med": "jama internal medicine",
    "lancet": "the lancet",
    "lancet oncol": "the lancet oncology",
    "lancet infect dis": "the lancet infectious diseases",
    "bmj": "bmj",
    "ann surg": "annals of surgery",
    "ann surg oncol": "annals of surgical oncology",
    "ann intern med": "annals of internal medicine",
    "dis colon rectum": "diseases of the colon & rectum",
    "surg endosc": "surgical endoscopy",
    "colorectal dis": "colorectal disease",
    "br j surg": "british journal of surgery",
    "am j surg": "the american journal of surgery",
    "j am coll surg": "journal of the american college of surgeons",
    "world j surg": "world journal of surgery",
    "j gastrointest surg": "journal of gastrointestinal surgery",
    "j surg oncol": "journal of surgical oncology",
    "j clin oncol": "journal of clinical oncology",
    "cancer": "cancer",
    "ca cancer j clin": "ca: a cancer journal for clinicians",
    "nature": "nature",
    "nat med": "nature medicine",
    "science": "science",
    "cell": "cell",
    "plos one": "plos one",
    "plos med": "plos medicine",
    "proc natl acad sci u s a": "proceedings of the national academy of sciences",
    "pnas": "proceedings of the national academy of sciences",
}

_JOURNAL_FULL_TO_ABBREV: dict[str, str] = {v: k for k, v in _JOURNAL_ABBREV_TO_FULL.items()}


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


def _nfkd_fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def normalize_doi(doi: str) -> str:
    """Strip prefixes (https://doi.org/, http://doi.org/, doi:), lowercase, strip whitespace."""
    if doi is None:
        return ""
    s = doi.strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/", "doi:"):
        if s.lower().startswith(prefix):
            s = s[len(prefix):]
            break
    return s.strip().lower()


_LEADING_ARTICLES = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)


def normalize_title(title: str) -> str:
    """Lowercase, NFKD-fold, strip punctuation, collapse whitespace, drop leading article."""
    if title is None:
        return ""
    s = _nfkd_fold(title).lower()
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    s = _LEADING_ARTICLES.sub("", s)
    return s


def parse_author_string(name: str) -> tuple[str, str]:
    """Return (surname, given_initials) tuple. Handle 'Last, F M' and 'F M Last' formats."""
    if name is None:
        return ("", "")
    s = name.strip()
    if not s:
        return ("", "")
    if "," in s:
        parts = s.split(",", 1)
        surname = parts[0].strip()
        given = parts[1].strip() if len(parts) > 1 else ""
    else:
        tokens = s.split()
        if len(tokens) == 1:
            surname = tokens[0]
            given = ""
        else:
            surname = tokens[-1]
            given = " ".join(tokens[:-1])
    return surname, given


def normalize_author_surname(name: str) -> str:
    """Return lowercased, NFKD-folded, punctuation-stripped surname only.

    Hyphens, em-dashes, etc. inside the surname become spaces so that
    'García-López' → 'garcia lopez'.
    """
    if name is None:
        return ""
    surname, _ = parse_author_string(name)
    s = _nfkd_fold(surname).lower()
    s = re.sub(r"[\-\–\—_/]", " ", s)
    s = re.sub(r"[^\w\s]", "", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize_journal(name: str) -> str:
    """Lowercase, NFKD-fold, strip punctuation, collapse whitespace, drop leading article."""
    if name is None:
        return ""
    s = _nfkd_fold(name).lower()
    s = re.sub(r"[^\w\s&]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    s = _LEADING_ARTICLES.sub("", s)
    return s


def journals_match(input_journal: str, candidate_journal: str) -> bool:
    """True if journals match exactly OR via the abbreviation lookup."""
    a = normalize_journal(input_journal)
    b = normalize_journal(candidate_journal)
    if not a or not b:
        return False
    if a == b:
        return True
    if _JOURNAL_ABBREV_TO_FULL.get(a) == b:
        return True
    if _JOURNAL_ABBREV_TO_FULL.get(b) == a:
        return True
    if _JOURNAL_FULL_TO_ABBREV.get(a) == b:
        return True
    if _JOURNAL_FULL_TO_ABBREV.get(b) == a:
        return True
    return False


# ---------------------------------------------------------------------------
# Layer 1 — identifier-decisive
# ---------------------------------------------------------------------------


_ARXIV_VERSION_RE = re.compile(r"v\d+$", re.IGNORECASE)


def _normalize_arxiv(arxiv_id: str) -> str:
    if not arxiv_id:
        return ""
    s = arxiv_id.strip().lower()
    for prefix in ("arxiv:",):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    s = _ARXIV_VERSION_RE.sub("", s)
    return s.strip()


def _normalize_pmid(pmid: str | int) -> str:
    if pmid is None:
        return ""
    return str(pmid).strip().lstrip("0") or "0"


def _normalize_simple_id(val: str) -> str:
    if not val:
        return ""
    return str(val).strip().lower()


def check_identifier_match(input_citation: dict, candidate: dict) -> dict | None:
    """Layer 1: return decisive match if any identifier matches, else None."""
    checks = [
        ("doi", normalize_doi),
        ("pmid", _normalize_pmid),
        ("arxiv_id", _normalize_arxiv),
        ("openalex_id", _normalize_simple_id),
        ("semantic_scholar_id", _normalize_simple_id),
    ]
    for key, normalizer in checks:
        in_val = input_citation.get(key)
        cand_val = candidate.get(key)
        if not in_val or not cand_val:
            continue
        if normalizer(in_val) == normalizer(cand_val):
            return {
                "confidence": 1.0,
                "match_quality": "high",
                "matched_identifier": key,
            }
    return None


# ---------------------------------------------------------------------------
# Layer 2 — weighted-field score
# ---------------------------------------------------------------------------


_DEFAULT_WEIGHTS = {
    "title": 0.40,
    "first_author": 0.20,
    "year": 0.20,
    "journal": 0.10,
    "other_authors": 0.10,
}


def _title_similarity(input_title: str, candidate_title: str) -> float:
    a = normalize_title(input_title)
    b = normalize_title(candidate_title)
    if not a or not b:
        return 0.0
    return fuzz.token_sort_ratio(a, b) / 100.0


def _year_score(input_year: int | None, candidate_year: int | None) -> float:
    if input_year is None or candidate_year is None:
        return 0.0
    delta = abs(int(input_year) - int(candidate_year))
    if delta == 0:
        return 1.0
    if delta == 1:
        return 0.7
    return 0.0


def _first_author_match(input_authors: list, candidate_authors: list) -> float:
    if not input_authors or not candidate_authors:
        return 0.0
    in_surname = normalize_author_surname(_author_to_string(input_authors[0]))
    cand_surname = normalize_author_surname(_author_to_string(candidate_authors[0]))
    if not in_surname or not cand_surname:
        return 0.0
    return 1.0 if in_surname == cand_surname else 0.0


def _other_authors_match(input_authors: list, candidate_authors: list) -> float:
    if not input_authors or len(input_authors) < 2:
        return 0.0
    if not candidate_authors:
        return 0.0
    cited_others = [normalize_author_surname(_author_to_string(a)) for a in input_authors[1:]]
    cited_others = [s for s in cited_others if s]
    if not cited_others:
        return 0.0
    cand_surnames = {normalize_author_surname(_author_to_string(a)) for a in candidate_authors}
    cand_surnames.discard("")
    if not cand_surnames:
        return 0.0
    hits = sum(1 for s in cited_others if s in cand_surnames)
    return hits / len(cited_others)


def _author_to_string(a: Any) -> str:
    """Accept str ('Last, F') or dict ({family, given})."""
    if isinstance(a, str):
        return a
    if isinstance(a, dict):
        family = (a.get("family") or "").strip()
        given = (a.get("given") or "").strip()
        if family and given:
            return f"{family}, {given}"
        return family or given or ""
    return ""


def _count_populated_input_fields(input_citation: dict) -> int:
    """Phase 1.B refinement 6.1: count populated fields among {title, first_author, year, journal}."""
    n = 0
    if input_citation.get("title"):
        n += 1
    authors = input_citation.get("authors") or []
    if authors:
        n += 1
    if input_citation.get("year") is not None:
        n += 1
    if input_citation.get("journal"):
        n += 1
    return n


def score_bibliographic_match(input_citation: dict, candidate: dict) -> dict:
    """Layer 2: weighted-field score with sanity guards."""
    input_title = input_citation.get("title") or ""
    cand_title = candidate.get("title") or ""
    title_sim = _title_similarity(input_title, cand_title)

    input_authors = input_citation.get("authors") or []
    cand_authors = candidate.get("authors") or []
    first_author_match = _first_author_match(input_authors, cand_authors)
    other_authors_match = _other_authors_match(input_authors, cand_authors)

    input_year = input_citation.get("year")
    cand_year = candidate.get("year")
    year_match = _year_score(input_year, cand_year)

    input_journal = input_citation.get("journal")
    journal_match = 1.0 if journals_match(input_journal, candidate.get("journal")) else 0.0

    applicable = {
        "title": bool(input_title),
        "first_author": bool(input_authors),
        "year": input_year is not None,
        "journal": bool(input_journal),
        "other_authors": isinstance(input_authors, list) and len(input_authors) >= 2,
    }

    weights = dict(_DEFAULT_WEIGHTS)
    for k, ok in applicable.items():
        if not ok:
            weights[k] = 0.0
    applicable_sum = sum(weights.values())
    if applicable_sum > 0:
        weights = {k: v / applicable_sum for k, v in weights.items()}

    # Short-title adjustment — applied AFTER input-driven redistribution so the
    # "title weight = 0.20 when short" invariant holds. Freed weight is
    # redistributed equally across other applicable fields.
    normalized_input_title = normalize_title(input_title)
    token_count = len(normalized_input_title.split()) if normalized_input_title else 0
    if token_count > 0 and token_count < 5 and applicable["title"]:
        other_applicable = [k for k, ok in applicable.items() if ok and k != "title"]
        if other_applicable:
            freed = weights["title"] - 0.20
            weights["title"] = 0.20
            share = freed / len(other_applicable)
            for k in other_applicable:
                weights[k] += share

    weighted_score = (
        weights["title"] * title_sim
        + weights["first_author"] * first_author_match
        + weights["year"] * year_match
        + weights["journal"] * journal_match
        + weights["other_authors"] * other_authors_match
    )

    # --- Hard sanity guards. Apply in order; first hit wins. ---
    rejected_by: str | None = None
    if input_year is not None and cand_year is not None and abs(int(input_year) - int(cand_year)) > 1:
        rejected_by = "year_off_by_more_than_one"
    elif (
        input_authors
        and cand_authors
        and first_author_match < 1.0
        and title_sim < 0.95
    ):
        rejected_by = "first_author_mismatch_low_title_sim"
    elif input_title and title_sim < 0.70:
        rejected_by = "title_similarity_below_floor"

    score_breakdown = {
        "title_sim": round(title_sim, 4),
        "first_author_match": first_author_match,
        "year_match": year_match,
        "journal_match": journal_match,
        "other_authors_match": round(other_authors_match, 4),
        "weighted_score": round(weighted_score, 4),
        "weights_applied": weights,
        "rejected_by": rejected_by,
    }

    if rejected_by:
        # Phase 1.B refinement 6.2: rejected_by lives ONLY in score_breakdown.
        return {
            "confidence": 0.0,
            "match_quality": "none",
            "score_breakdown": score_breakdown,
            "requires_review": False,
        }

    if weighted_score >= 0.90 and title_sim >= 0.95:
        match_quality = "high"
        requires_review = False
    elif weighted_score >= 0.75:
        match_quality = "medium"
        requires_review = False
    elif weighted_score >= 0.60:
        match_quality = "low"
        requires_review = True
    else:
        match_quality = "none"
        requires_review = False

    # Phase 1.B refinement 6.1: cap at "medium" if <3 populated input fields.
    populated = _count_populated_input_fields(input_citation)
    capped = False
    if populated < 3 and match_quality == "high":
        match_quality = "medium"
        capped = True
        score_breakdown["capped_at_medium_insufficient_input_fields"] = True

    return {
        "confidence": round(weighted_score, 4),
        "match_quality": match_quality,
        "score_breakdown": score_breakdown,
        "requires_review": requires_review,
        "capped_at_medium_insufficient_input_fields": capped,
    }


def score_match(input_citation: dict, candidate: dict) -> dict:
    """Combined entry point: Layer 1 first, then Layer 2."""
    layer1 = check_identifier_match(input_citation, candidate)
    if layer1 is not None:
        return {
            "confidence": layer1["confidence"],
            "match_quality": layer1["match_quality"],
            "score_breakdown": {
                "layer": "identifier",
                "matched_identifier": layer1["matched_identifier"],
            },
            "requires_review": False,
            "capped_at_medium_insufficient_input_fields": False,
        }
    return score_bibliographic_match(input_citation, candidate)


# ---------------------------------------------------------------------------
# Layer 3 — canonical record merging across multi-DB matches
# ---------------------------------------------------------------------------


_AUTHORITY: dict[str, list[str]] = {
    "doi": ["crossref", "openalex", "semantic_scholar", "pubmed", "arxiv"],
    "pmid": ["pubmed", "crossref", "openalex", "semantic_scholar"],
    "arxiv_id": ["arxiv", "semantic_scholar"],
    "openalex_id": ["openalex"],
    "paper_id": ["semantic_scholar"],
    "title": ["crossref", "pubmed", "openalex", "semantic_scholar", "arxiv"],
    "authors": ["crossref", "openalex", "pubmed", "semantic_scholar", "arxiv"],
    "journal": ["crossref", "openalex", "pubmed", "semantic_scholar"],
    "journal_iso_abbrev": ["pubmed"],
    "volume": ["crossref", "pubmed", "openalex"],
    "issue": ["crossref", "pubmed", "openalex"],
    "pages": ["crossref", "pubmed", "openalex"],
    "abstract": ["pubmed", "semantic_scholar", "openalex"],
    "type": ["crossref", "openalex", "pubmed"],
}


def _by_source(records: list[dict]) -> dict[str, dict]:
    """Group input records by their `source` field. Last write wins on duplicates."""
    out: dict[str, dict] = {}
    for r in records:
        if not isinstance(r, dict):
            continue
        src = r.get("source")
        if not src:
            continue
        out[src] = r
    return out


_PAGES_FIRST_INT_RE = re.compile(r"\d+")


def _pages_first_int(pages: str | None) -> int | None:
    if not pages:
        return None
    m = _PAGES_FIRST_INT_RE.search(str(pages))
    if not m:
        return None
    try:
        return int(m.group(0))
    except ValueError:
        return None


def _norm_string_for_compare(s: Any) -> str:
    if s is None:
        return ""
    s = _nfkd_fold(str(s)).lower()
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _authors_to_surname_list(authors: list | None) -> list[str]:
    if not authors:
        return []
    out: list[str] = []
    for a in authors:
        sn = normalize_author_surname(_author_to_string(a))
        if sn:
            out.append(sn)
    return out


def _values_disagree(field: str, canonical: Any, other: Any) -> bool:
    """Field-specific equality test. True if `other` disagrees with `canonical`."""
    if canonical is None or other is None:
        return False
    if field in {"doi", "pmid", "arxiv_id", "openalex_id", "paper_id"}:
        a = _normalize_simple_id(str(canonical))
        b = _normalize_simple_id(str(other))
        if field == "doi":
            a = normalize_doi(str(canonical))
            b = normalize_doi(str(other))
        elif field == "pmid":
            a = _normalize_pmid(canonical)
            b = _normalize_pmid(other)
        elif field == "arxiv_id":
            a = _normalize_arxiv(str(canonical))
            b = _normalize_arxiv(str(other))
        return a != b
    if field == "title":
        return _norm_string_for_compare(canonical) != _norm_string_for_compare(other)
    if field == "journal":
        return not journals_match(str(canonical), str(other))
    if field == "journal_iso_abbrev":
        return _norm_string_for_compare(canonical) != _norm_string_for_compare(other)
    if field == "authors":
        return _authors_to_surname_list(canonical) != _authors_to_surname_list(other)
    if field == "type":
        return _norm_string_for_compare(canonical) != _norm_string_for_compare(other)
    if field in {"volume", "issue"}:
        return _norm_string_for_compare(canonical) != _norm_string_for_compare(other)
    if field == "pages":
        a = _pages_first_int(canonical)
        b = _pages_first_int(other)
        if a is None or b is None:
            return False
        return a != b
    if field == "abstract":
        return _norm_string_for_compare(canonical) != _norm_string_for_compare(other)
    return canonical != other


def _resolve_by_authority(
    field: str,
    by_source: dict[str, dict],
    authority: list[str],
) -> tuple[Any, str | None, list[tuple[str, Any]]]:
    """Walk authority order; return (canonical_value, canonical_source, others_nonnull).

    `others_nonnull` are (source, value) tuples for lower-authority DBs that
    supplied a non-null value — used to detect disagreements.
    """
    canonical: Any = None
    canonical_source: str | None = None
    others: list[tuple[str, Any]] = []
    for src in authority:
        rec = by_source.get(src)
        if not rec:
            continue
        val = rec.get(field)
        if val is None or (isinstance(val, (list, str)) and len(val) == 0):
            continue
        if canonical is None:
            canonical = val
            canonical_source = src
        else:
            others.append((src, val))
    return canonical, canonical_source, others


def _earliest_year(by_source: dict[str, dict]) -> tuple[int | None, dict[str, int]]:
    """Return (earliest_year, per_db_years)."""
    ys: dict[str, int] = {}
    for src, rec in by_source.items():
        y = rec.get("year")
        if isinstance(y, int):
            ys[src] = y
    if not ys:
        return None, {}
    return min(ys.values()), ys


def _citation_counts(by_source: dict[str, dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for src in ("openalex", "semantic_scholar"):
        rec = by_source.get(src)
        if rec and isinstance(rec.get("citation_count"), int):
            out[src] = rec["citation_count"]
    return out


def merge_canonical_records(records: list[dict]) -> dict:
    """Layer 3: merge per-DB normalized records into a canonical + discrepancy list.

    Returns {"canonical": dict, "discrepancies": list[dict]}.
    """
    by_source = _by_source(records)
    canonical: dict[str, Any] = {}
    discrepancies: list[dict] = []

    for field, authority in _AUTHORITY.items():
        value, source, others = _resolve_by_authority(field, by_source, authority)
        canonical[field] = value
        if value is not None:
            disagreeing: dict[str, Any] = {}
            for src, other_val in others:
                if _values_disagree(field, value, other_val):
                    disagreeing[src] = other_val
            if disagreeing:
                values_map: dict[str, Any] = {source: value} if source else {}
                values_map.update(disagreeing)
                discrepancies.append({
                    "field": field,
                    "values": values_map,
                    "resolved_to": value,
                    "rule": "highest_authority_db",
                })

    # Year: earliest non-null across all DBs.
    earliest, per_db_years = _earliest_year(by_source)
    canonical["year"] = earliest
    if earliest is not None and per_db_years:
        disagreeing = {
            src: y for src, y in per_db_years.items()
            if abs(y - earliest) > 1
        }
        if disagreeing:
            values_map = dict(per_db_years)
            discrepancies.append({
                "field": "year",
                "values": values_map,
                "resolved_to": earliest,
                "rule": "earliest_non_null_year",
            })

    # Citation count: report both, flag if >20% relative difference.
    counts = _citation_counts(by_source)
    if counts:
        canonical["citation_count"] = counts
        oa = counts.get("openalex")
        ss = counts.get("semantic_scholar")
        if oa is not None and ss is not None and max(oa, ss) > 0:
            denom = max(oa, ss)
            if abs(oa - ss) / denom > 0.20:
                discrepancies.append({
                    "field": "citation_count",
                    "values": counts,
                    "resolved_to": counts,
                    "rule": "report_both_diff_gt_20pct",
                })
    else:
        canonical["citation_count"] = None

    # Sources list, for traceability.
    canonical["sources"] = sorted(by_source.keys())

    return {"canonical": canonical, "discrepancies": discrepancies}
