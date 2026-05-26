"""Tests for Layer 3 canonical record merging."""

from __future__ import annotations

from citation_mcp.scoring import merge_canonical_records


def _make_record(source: str, **fields):
    base = {"source": source}
    base.update(fields)
    return base


def test_full_agreement_no_discrepancies():
    records = [
        _make_record("crossref", doi="10.1/x", title="T", year=2020, journal="J",
                     authors=[{"family": "Smith", "given": "J"}]),
        _make_record("pubmed", doi="10.1/x", title="T", year=2020,
                     journal="J", authors=[{"family": "Smith", "given": "J"}],
                     pmid="123"),
        _make_record("openalex", doi="10.1/x", title="T", year=2020,
                     journal="J", authors=[{"family": "Smith", "given": "J"}],
                     openalex_id="W1"),
    ]
    out = merge_canonical_records(records)
    assert out["discrepancies"] == []
    assert out["canonical"]["title"] == "T"
    assert out["canonical"]["doi"] == "10.1/x"
    assert out["canonical"]["openalex_id"] == "W1"
    assert out["canonical"]["pmid"] == "123"
    assert out["canonical"]["year"] == 2020
    assert set(out["canonical"]["sources"]) == {"crossref", "pubmed", "openalex"}


def test_year_disagreement_earliest_wins_and_flagged():
    records = [
        _make_record("crossref", year=2020, doi="10.1/x"),
        _make_record("pubmed", year=2020, doi="10.1/x"),
        _make_record("openalex", year=2020, doi="10.1/x"),
        _make_record("semantic_scholar", year=2023, doi="10.1/x"),
    ]
    out = merge_canonical_records(records)
    assert out["canonical"]["year"] == 2020
    year_disc = [d for d in out["discrepancies"] if d["field"] == "year"]
    assert len(year_disc) == 1
    assert year_disc[0]["resolved_to"] == 2020
    assert year_disc[0]["values"]["semantic_scholar"] == 2023


def test_year_one_year_diff_not_flagged():
    records = [
        _make_record("crossref", year=2020),
        _make_record("openalex", year=2021),
    ]
    out = merge_canonical_records(records)
    assert out["canonical"]["year"] == 2020
    assert not any(d["field"] == "year" for d in out["discrepancies"])


def test_citation_count_within_20pct_no_discrepancy():
    records = [
        _make_record("openalex", citation_count=1000, doi="10.1/x"),
        _make_record("semantic_scholar", citation_count=900, doi="10.1/x"),
    ]
    out = merge_canonical_records(records)
    assert out["canonical"]["citation_count"] == {"openalex": 1000, "semantic_scholar": 900}
    assert not any(d["field"] == "citation_count" for d in out["discrepancies"])


def test_citation_count_over_20pct_flagged():
    records = [
        _make_record("openalex", citation_count=1000, doi="10.1/x"),
        _make_record("semantic_scholar", citation_count=500, doi="10.1/x"),
    ]
    out = merge_canonical_records(records)
    citation_disc = [d for d in out["discrepancies"] if d["field"] == "citation_count"]
    assert len(citation_disc) == 1
    assert citation_disc[0]["values"] == {"openalex": 1000, "semantic_scholar": 500}


def test_author_list_discrepancy_normalized():
    records = [
        _make_record("crossref", doi="10.1/x",
                     authors=[{"family": "Smith", "given": "J"},
                              {"family": "Jones", "given": "K"}]),
        _make_record("semantic_scholar", doi="10.1/x",
                     authors=[{"family": "Smith", "given": "John"}]),  # missing Jones
    ]
    out = merge_canonical_records(records)
    author_disc = [d for d in out["discrepancies"] if d["field"] == "authors"]
    assert len(author_disc) == 1
    assert out["canonical"]["authors"][0]["family"] == "Smith"  # crossref wins


def test_abstract_only_in_one_db():
    records = [
        _make_record("crossref", doi="10.1/x"),
        _make_record("pubmed", doi="10.1/x", abstract="Background: ..."),
    ]
    out = merge_canonical_records(records)
    assert out["canonical"]["abstract"].startswith("Background:")
    # No discrepancy because no DB disagrees with PubMed's value.
    assert not any(d["field"] == "abstract" for d in out["discrepancies"])


def test_journal_iso_abbrev_from_pubmed_only():
    records = [
        _make_record("crossref", journal="New England Journal of Medicine"),
        _make_record("pubmed", journal="The New England journal of medicine",
                     journal_iso_abbrev="N Engl J Med"),
    ]
    out = merge_canonical_records(records)
    assert out["canonical"]["journal_iso_abbrev"] == "N Engl J Med"
    # journals_match should treat these as equivalent — no discrepancy.
    assert not any(d["field"] == "journal" for d in out["discrepancies"])


def test_doi_authority_crossref_first():
    records = [
        _make_record("openalex", doi="10.2/wrong"),
        _make_record("crossref", doi="10.1/right"),
    ]
    out = merge_canonical_records(records)
    assert out["canonical"]["doi"] == "10.1/right"
    doi_disc = [d for d in out["discrepancies"] if d["field"] == "doi"]
    assert len(doi_disc) == 1
    assert doi_disc[0]["resolved_to"] == "10.1/right"
    assert doi_disc[0]["values"]["openalex"] == "10.2/wrong"


def test_pages_em_dash_tolerated():
    records = [
        _make_record("crossref", pages="2603-2615"),
        _make_record("pubmed", pages="2603–2615"),  # en-dash
    ]
    out = merge_canonical_records(records)
    assert out["canonical"]["pages"] == "2603-2615"
    assert not any(d["field"] == "pages" for d in out["discrepancies"])


def test_pages_first_int_mismatch_flagged():
    records = [
        _make_record("crossref", pages="2603-2615"),
        _make_record("pubmed", pages="9999-9999"),
    ]
    out = merge_canonical_records(records)
    pages_disc = [d for d in out["discrepancies"] if d["field"] == "pages"]
    assert len(pages_disc) == 1
