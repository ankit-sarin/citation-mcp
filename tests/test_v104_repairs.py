"""v1.0.4 repairs (CM-106D) — fail-first tests for FIX A/B/C and the R5 regression set.

FIX A (cache.py)   non-string input values never make make_cache_key raise; string keys unchanged
FIX B (server.py)  same-work filter: a title empty after normalisation counts as absent
FIX C (scoring.py) empty / whitespace-only input authors are ignored before any author check
R5 regression set  no-title kept; untitled best keeps all; subtitle allowance in the filter;
                   0.90 boundary; version strings agree; golden cache key; bulk isolation
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

import citation_mcp
from citation_mcp.cache import make_cache_key
from citation_mcp.scoring import score_bibliographic_match
from citation_mcp.server import bulk_verify_citations, verify_citation

ROOT = Path(__file__).resolve().parents[1]


def rec(source: str, **fields) -> dict:
    base = {
        "source": source, "doi": None, "pmid": None, "arxiv_id": None,
        "openalex_id": None, "paper_id": None, "title": None, "authors": None,
        "journal": None, "year": None, "citation_count": None, "type": None,
    }
    base.update(fields)
    return base


class FakeClient:
    def __init__(self, *, doi=None, by_doi=None, metadata=None):
        self._doi = doi
        self._by_doi = by_doi or {}
        self._metadata = metadata or []
        self.enabled = True

    async def search_by_doi(self, doi):
        return self._by_doi.get(doi, self._doi)

    async def search_by_pmid(self, pmid):
        return None

    async def search_by_arxiv_id(self, arxiv_id):
        return None

    async def search_by_metadata(self, **kwargs):
        return list(self._metadata)


def fakes(**kw):
    out = {k: FakeClient() for k in ("crossref", "pubmed", "openalex", "semantic_scholar", "arxiv")}
    out.update(kw)
    return out


async def run(inp, cache, **kw):
    c = fakes(**kw)
    return await verify_citation(
        inp, c["crossref"], cache, pubmed=c["pubmed"], openalex=c["openalex"],
        semantic_scholar=c["semantic_scholar"], arxiv=c["arxiv"], force_refresh=True,
    )


DOI = "10.1/same"


async def doi_pair(cache, cr_title, pm_title):
    """DOI input: both DBs match by identifier (confidence 1.0); Crossref is best by order."""
    return await run({"doi": DOI}, cache,
                     crossref=FakeClient(doi=rec("crossref", doi=DOI, title=cr_title)),
                     pubmed=FakeClient(doi=rec("pubmed", doi=DOI, pmid="123", title=pm_title)))


def merged(res):
    return [w["message"] for w in res["warnings"] if w.get("source") == "merge"]


# ---------------------------------------------------------------------------
# FIX A — non-string values in make_cache_key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field,value", [
    ("doi", 1056), ("pmid", 15496622), ("pmid", 1.5), ("arxiv_id", 1706.03762), ("title", 5),
    ("year", 2020.0), ("journal", 5), ("volume", 383), ("issue", 27.0), ("pages", 2603),
    ("authors", [5, 6.5]), ("authors", [{"family": 5, "given": 6}]), ("authors", 5),
])
def test_fix_a_non_string_value_does_not_raise(field, value):
    base = {"doi": "10.1/x"} if field != "doi" else {}
    key = make_cache_key({**base, field: value})
    assert key.startswith("verify:")


def test_fix_a_non_string_values_still_distinguish():
    assert make_cache_key({"doi": "10.1/x", "title": 5}) != make_cache_key({"doi": "10.1/x", "title": 6})


async def test_r5_bulk_item_with_non_string_title_does_not_sink_the_batch(cache):
    """v1.0.3 handled {doi, title: 5} via the identifier path; c8925d7 raised in the key builder
    and, with gather() not catching, aborted every item in the batch."""
    cr = FakeClient(by_doi={"10.1/a": rec("crossref", doi="10.1/a", title="A paper"),
                            "10.1/b": rec("crossref", doi="10.1/b", title="B paper")})
    c = fakes(crossref=cr)
    out = await bulk_verify_citations(
        [{"doi": "10.1/a", "title": 5}, {"doi": "10.1/b", "title": "B paper"}],
        c["crossref"], cache, pubmed=c["pubmed"], openalex=c["openalex"],
        semantic_scholar=c["semantic_scholar"], arxiv=c["arxiv"], force_refresh=True,
    )
    assert out["summary"]["total"] == 2
    assert [r["canonical"]["doi"] for r in out["results"]] == ["10.1/a", "10.1/b"]
    assert all(r["verified"] for r in out["results"])


# Golden key: computed at c8925d7 (CM-106B) for this exact input; must never drift silently.
GOLDEN_INPUT = {
    "doi": "https://doi.org/10.1056/NEJMoa2034577",
    "title": "Safety and Efficacy of the BNT162b2 mRNA Covid-19 Vaccine",
    "authors": ["Polack, Fernando P.", "Thomas SJ"], "year": 2020, "journal": "N Engl J Med",
    "volume": "383", "issue": "27", "pages": "2603-2615",
}
GOLDEN_KEY = "verify:doi:4eec7ac441acda1372d7d419e7028bb9e009efcff3bea7e2b737931a8f3f20eb"


def test_r5_golden_cache_key():
    assert make_cache_key(GOLDEN_INPUT) == GOLDEN_KEY


# ---------------------------------------------------------------------------
# FIX B — blank titles in the same-work filter
# ---------------------------------------------------------------------------

REAL = "Autonomous robotic laparoscopic surgery for intestinal anastomosis"


@pytest.mark.parametrize("blank", ["   ", "...", " - : "])
async def test_fix_b_other_record_with_blank_title_is_kept(cache, blank):
    res = await doi_pair(cache, REAL, blank)
    assert res["databases_confirmed"] == ["crossref", "pubmed"]
    assert merged(res) == []


@pytest.mark.parametrize("blank", ["   ", "...", " - : "])
async def test_fix_b_best_record_with_blank_title_keeps_all(cache, blank):
    res = await doi_pair(cache, blank, REAL)
    assert res["databases_confirmed"] == ["crossref", "pubmed"]
    assert res["canonical"]["pmid"] == "123"
    assert merged(res) == []


async def test_r5_guard_record_with_no_title_is_kept(cache):
    res = await doi_pair(cache, REAL, None)
    assert res["databases_confirmed"] == ["crossref", "pubmed"]
    assert merged(res) == []


async def test_r5_guard_best_record_with_no_title_keeps_all(cache):
    res = await doi_pair(cache, None, REAL)
    assert res["databases_confirmed"] == ["crossref", "pubmed"]
    assert merged(res) == []


async def test_r5_guard_same_work_filter_honours_subtitle_allowance(cache):
    """sim 0.661 (< 0.90) but the Crossref title is the PubMed title's main part."""
    full = "Recurrent secondary hyperparathyroidism: an argument for total parathyroidectomy"
    res = await doi_pair(cache, "Recurrent Secondary Hyperparathyroidism", full)
    assert res["databases_confirmed"] == ["crossref", "pubmed"]
    assert merged(res) == []


@pytest.mark.parametrize("cr_title,pm_title,kept", [
    # 0.9042 — kept
    ("Endoscopic ultrasound fine‐needle aspiration versus fine‐needle biopsy for lymph node "
     "diagnosis: A large multicenter comparative analysis",
     "Su1192 ENDOSCOPIC ULTRASOUND FINE NEEDLE ASPIRATION VERSUS FINE NEEDLE BIOPSY FOR LYMPH NODE "
     "DIAGNOSIS: A COMPARATIVE ANALYSIS", True),
    # 0.8926 — excluded
    ("Laparoscopic versus open colectomy for colon cancer outcomes",
     "Laparoscopic versus open colectomy for rectal cancer outcomes", False),
])
async def test_r5_guard_same_work_filter_at_090_boundary(cache, cr_title, pm_title, kept):
    res = await doi_pair(cache, cr_title, pm_title)
    if kept:
        assert res["databases_confirmed"] == ["crossref", "pubmed"]
        assert merged(res) == []
    else:
        assert res["databases_confirmed"] == ["crossref"]
        assert merged(res) == ["excluded_pubmed_title_disagrees_with_best_match"]


# ---------------------------------------------------------------------------
# FIX C — empty input authors
# ---------------------------------------------------------------------------

ELDERLY = "Outcomes of robotic colectomy in elderly patients"
OLDER = "Outcomes of robotic colectomy in older patients"  # 0.8542


@pytest.mark.parametrize("authors", [[""], ["   "], [{}], [{"family": " ", "given": ""}], ["", " "]])
def test_fix_c_all_empty_authors_is_a_no_author_input(authors):
    r = score_bibliographic_match({"title": ELDERLY, "authors": authors}, {"title": OLDER})
    assert r["score_breakdown"]["rejected_by"] == "title_similarity_below_floor"
    assert r["score_breakdown"]["weights_applied"]["first_author"] == 0.0


def test_fix_c_leading_empty_author_is_ignored_for_first_author():
    r = score_bibliographic_match({"title": ELDERLY, "authors": ["", "Smith J"], "year": 2020},
                                  {"title": OLDER, "year": 2020,
                                   "authors": [{"family": "Smith", "given": "J"}]})
    assert r["score_breakdown"]["first_author_match"] == 1.0
    assert r["score_breakdown"]["rejected_by"] is None


# ---------------------------------------------------------------------------
# R5 — version strings agree
# ---------------------------------------------------------------------------


def test_r5_guard_four_version_strings_agree():
    v = citation_mcp.__version__
    assert tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"] == v
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    assert [p["version"] for p in lock["package"] if p["name"] == "citation-mcp"] == [v]
    cff = re.search(r"^version:\s*(\S+)\s*$", (ROOT / "CITATION.cff").read_text(), re.M)
    assert cff and cff.group(1) == v
