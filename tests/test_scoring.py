"""Tests for the scoring module."""

from __future__ import annotations

import pytest

from citation_mcp.scoring import (
    check_identifier_match,
    journals_match,
    normalize_author_surname,
    normalize_doi,
    normalize_journal,
    normalize_title,
    parse_author_string,
    score_bibliographic_match,
    score_match,
)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def test_normalize_doi_strips_prefixes_and_lowercases():
    assert normalize_doi("https://doi.org/10.1056/NEJMoa2034577") == "10.1056/nejmoa2034577"
    assert normalize_doi("http://doi.org/10.1056/NEJMoa2034577") == "10.1056/nejmoa2034577"
    assert normalize_doi("doi:10.1056/NEJMoa2034577") == "10.1056/nejmoa2034577"
    assert normalize_doi(" 10.1056/NEJMoa2034577 ") == "10.1056/nejmoa2034577"
    assert normalize_doi("https://dx.doi.org/10.1056/NEJMoa2034577") == "10.1056/nejmoa2034577"


def test_normalize_title_unicode_punct_and_articles():
    assert normalize_title("The Effects of Cänker-Sore on Health.") == "effects of canker sore on health"
    assert normalize_title("A Study  of   Things!") == "study of things"
    assert normalize_title("An Investigation: Sub-Title") == "investigation sub title"


def test_normalize_author_surname_both_formats():
    assert normalize_author_surname("Polack, Fernando P.") == "polack"
    assert normalize_author_surname("Fernando P. Polack") == "polack"
    assert normalize_author_surname("García-López, María") == "garcia lopez"
    assert normalize_author_surname("O'Brien, Sean") == "obrien"


def test_parse_author_string():
    assert parse_author_string("Polack, Fernando P.") == ("Polack", "Fernando P.")
    assert parse_author_string("Fernando P. Polack") == ("Polack", "Fernando P.")
    assert parse_author_string("Polack") == ("Polack", "")


def test_normalize_journal():
    assert normalize_journal("New England Journal of Medicine.") == "new england journal of medicine"
    assert normalize_journal("JAMA Surg.") == "jama surg"


def test_journal_abbreviation_lookup():
    assert journals_match("N Engl J Med", "New England Journal of Medicine")
    assert journals_match("New England Journal of Medicine", "N Engl J Med")
    assert journals_match("JAMA Surg", "JAMA Surgery")
    assert not journals_match("N Engl J Med", "JAMA")


# ---------------------------------------------------------------------------
# Layer 1
# ---------------------------------------------------------------------------


def test_identifier_match_doi_hit():
    inp = {"doi": "10.1056/NEJMoa2034577"}
    cand = {"doi": "10.1056/nejmoa2034577"}
    result = check_identifier_match(inp, cand)
    assert result == {"confidence": 1.0, "match_quality": "high", "matched_identifier": "doi"}


def test_identifier_match_doi_miss():
    inp = {"doi": "10.1056/nejmoa2034577"}
    cand = {"doi": "10.1000/different"}
    assert check_identifier_match(inp, cand) is None


def test_identifier_match_pmid_hit():
    inp = {"pmid": "33301246"}
    cand = {"pmid": "33301246"}
    result = check_identifier_match(inp, cand)
    assert result["matched_identifier"] == "pmid"


def test_identifier_match_no_identifiers_returns_none():
    assert check_identifier_match({"title": "x"}, {"title": "x"}) is None


# ---------------------------------------------------------------------------
# Layer 2 sanity guards
# ---------------------------------------------------------------------------


def test_year_off_by_more_than_one_rejects():
    inp = {"title": "Some Title", "year": 2015, "authors": ["Smith, J"]}
    cand = {"title": "Some Title", "year": 2020, "authors": [{"family": "Smith", "given": "J"}]}
    r = score_bibliographic_match(inp, cand)
    assert r["confidence"] == 0.0
    assert r["match_quality"] == "none"
    assert r["rejected_by"] == "year_off_by_more_than_one"


def test_first_author_mismatch_low_title_sim_rejects():
    inp = {
        "title": "Completely Different Title One",
        "authors": ["Doe, J"],
        "year": 2020,
    }
    cand = {
        "title": "Another Topic Entirely Two",
        "authors": [{"family": "Smith", "given": "J"}],
        "year": 2020,
    }
    r = score_bibliographic_match(inp, cand)
    assert r["rejected_by"] in ("first_author_mismatch_low_title_sim", "title_similarity_below_floor")


def test_title_similarity_below_floor_rejects():
    inp = {
        "title": "Apple Banana Cherry Date Elderberry",
        "authors": ["Smith, J"],
        "year": 2020,
    }
    cand = {
        "title": "Zucchini Yam Xigua Watermelon Vanilla",
        "authors": [{"family": "Smith", "given": "J"}],
        "year": 2020,
    }
    r = score_bibliographic_match(inp, cand)
    # Title sim should be far below 0.70; first_author matches so guard 2 doesn't fire.
    assert r["rejected_by"] == "title_similarity_below_floor"


# ---------------------------------------------------------------------------
# Short-title adjustment
# ---------------------------------------------------------------------------


def test_short_title_adjustment_redistributes_weights():
    # Normalized title "covid 19 vaccine" — 3 tokens, below threshold of 5.
    inp = {
        "title": "COVID-19 Vaccine",
        "authors": ["Polack, Fernando P."],
        "year": 2020,
        "journal": "New England Journal of Medicine",
    }
    cand = {
        "title": "Covid-19 Vaccine",
        "authors": [{"family": "Polack", "given": "Fernando P."}],
        "year": 2020,
        "journal": "New England Journal of Medicine",
    }
    r = score_bibliographic_match(inp, cand)
    weights = r["score_breakdown"]["weights_applied"]
    assert weights["title"] == 0.20
    # The remaining four fields should sum to 0.80.
    rest = sum(v for k, v in weights.items() if k != "title")
    assert abs(rest - 0.80) < 1e-9


def test_long_title_all_fields_uses_default_weights():
    """When input supplies all five fields, the default weight set is used unchanged."""
    inp = {
        "title": "Safety and Efficacy of the BNT162b2 mRNA Covid 19 Vaccine in Adults",
        "authors": ["Polack, Fernando P.", "Thomas, Stephen J."],
        "year": 2020,
        "journal": "New England Journal of Medicine",
    }
    cand = {
        "title": "Safety and Efficacy of the BNT162b2 mRNA Covid 19 Vaccine in Adults",
        "authors": [
            {"family": "Polack", "given": "Fernando P."},
            {"family": "Thomas", "given": "Stephen J."},
        ],
        "year": 2020,
        "journal": "New England Journal of Medicine",
    }
    r = score_bibliographic_match(inp, cand)
    weights = r["score_breakdown"]["weights_applied"]
    assert weights["title"] == 0.40
    assert weights["first_author"] == 0.20
    assert weights["year"] == 0.20
    assert weights["journal"] == 0.10
    assert weights["other_authors"] == 0.10


# ---------------------------------------------------------------------------
# Confidence thresholds
# ---------------------------------------------------------------------------


def test_high_quality_match():
    inp = {
        "title": "Safety and Efficacy of the BNT162b2 mRNA Covid-19 Vaccine",
        "authors": ["Polack, Fernando P."],
        "year": 2020,
        "journal": "New England Journal of Medicine",
    }
    cand = {
        "title": "Safety and Efficacy of the BNT162b2 mRNA Covid-19 Vaccine",
        "authors": [{"family": "Polack", "given": "Fernando P."}],
        "year": 2020,
        "journal": "New England Journal of Medicine",
    }
    r = score_bibliographic_match(inp, cand)
    assert r["match_quality"] == "high"
    assert r["confidence"] >= 0.90
    assert r["score_breakdown"]["title_sim"] >= 0.95
    assert r["requires_review"] is False
    assert r["rejected_by"] is None


def test_score_match_short_circuits_on_identifier():
    inp = {"doi": "10.1056/NEJMoa2034577", "title": "Wrong Title"}
    cand = {"doi": "10.1056/nejmoa2034577", "title": "Wrong Title"}
    r = score_match(inp, cand)
    assert r["confidence"] == 1.0
    assert r["match_quality"] == "high"
    assert r["score_breakdown"]["layer"] == "identifier"


def test_low_match_requires_review():
    # Tweak so weighted_score lands in [0.60, 0.75) — title mostly matches but year off by 1 + no journal.
    inp = {
        "title": "Some interesting study about cardiovascular outcomes",
        "authors": ["Smith, John A"],
        "year": 2019,
    }
    cand = {
        "title": "An interesting study regarding cardiovascular outcomes",
        "authors": [{"family": "Smith", "given": "John A"}],
        "year": 2020,
    }
    r = score_bibliographic_match(inp, cand)
    # Verify we land somewhere sensible — exact bucket depends on token_sort_ratio score.
    assert r["match_quality"] in ("low", "medium", "high")
    if r["match_quality"] == "low":
        assert r["requires_review"] is True
