"""v1.0.4 (CM-106B) fail-first tests for rulings R1–R6.

R1  no-author title floor (NO_AUTHOR_TITLE_FLOOR = 0.90, label title_similarity_below_floor)
R2  subtitle allowance (SUBTITLE_MIN_MAIN_WORDS = 3), used only by R1 and R3
R3  same-work merge filter before merge_canonical_records, one merge warning per excluded DB
R4  cache key covers every accepted input field
R5  equal-confidence tie-break prefers an identifier-layer score
R6  User-Agent literals follow the package version

Tests marked GUARD are non-regression guards: they pass before the fix by design
(they pin behaviour that must NOT change) and are reported as such.
"""

from __future__ import annotations

import httpx
import pytest

import citation_mcp
from citation_mcp.cache import make_cache_key
from citation_mcp.databases.arxiv import ArxivClient
from citation_mcp.databases.crossref import CrossrefClient
from citation_mcp.databases.pubmed import PubMedClient
from citation_mcp.scoring import score_bibliographic_match
from citation_mcp.server import verify_citation


def rec(source: str, **fields) -> dict:
    base = {
        "source": source, "doi": None, "pmid": None, "arxiv_id": None,
        "openalex_id": None, "paper_id": None, "title": None, "authors": None,
        "journal": None, "year": None, "citation_count": None, "type": None,
    }
    base.update(fields)
    return base


class FakeClient:
    def __init__(self, *, doi=None, pmid=None, arxiv=None, metadata=None, enabled=True):
        self._doi, self._pmid, self._arxiv = doi, pmid, arxiv
        self._metadata = metadata or []
        self.enabled = enabled

    async def search_by_doi(self, doi):
        return self._doi

    async def search_by_pmid(self, pmid):
        return self._pmid

    async def search_by_arxiv_id(self, arxiv_id):
        return self._arxiv

    async def search_by_metadata(self, **kwargs):
        return list(self._metadata)


def clients(**kw):
    """crossref is mandatory in verify_citation; default every DB to an empty fake."""
    out = {k: FakeClient() for k in ("crossref", "pubmed", "openalex", "semantic_scholar", "arxiv")}
    out.update(kw)
    return out


async def run(inp, cache, **kw):
    c = clients(**kw)
    return await verify_citation(
        inp, c["crossref"], cache, pubmed=c["pubmed"], openalex=c["openalex"],
        semantic_scholar=c["semantic_scholar"], arxiv=c["arxiv"], force_refresh=True,
    )


ATTN = "Attention Is All You Need"
LSD = rec("crossref", doi="10.5040/9781350101272", title="All You Need Is LSD",
          authors=[{"family": "Butler", "given": "Leo"}], year=2016)

TOP_KEYS = {"verified", "confidence", "match_quality", "databases_confirmed", "databases_queried",
            "databases_failed", "canonical", "discrepancies", "requires_review", "warnings",
            "score_breakdown"}
CANON_KEYS = {"doi", "pmid", "arxiv_id", "openalex_id", "paper_id", "title", "authors", "journal",
              "journal_iso_abbrev", "volume", "issue", "pages", "abstract", "type", "year",
              "citation_count", "sources"}
L2_KEYS = {"title_sim", "first_author_match", "year_match", "journal_match", "other_authors_match",
           "weighted_score", "weights_applied", "rejected_by"}


def merge_warnings(res):
    return [w for w in res["warnings"] if w.get("source") == "merge"]


# ---------------------------------------------------------------------------
# R1 / M2 — no-author floor
# ---------------------------------------------------------------------------


async def test_r1_title_year_no_author_lsd_lookalike_not_verified(cache):
    """CM-105A 2d: title + year, no author, Crossref's only hit is a 0.73 look-alike."""
    res = await run({"title": ATTN, "year": 2017}, cache, crossref=FakeClient(metadata=[LSD]))
    assert res["verified"] is False
    assert res["score_breakdown"]["rejected_by"] == "title_similarity_below_floor"
    assert res["canonical"]["title"] == ATTN  # echo of input, not "All You Need Is LSD"


async def test_r1_guard_title_year_no_author_exact_title_still_verified(cache):
    """GUARD: over-tightening check."""
    hit = rec("crossref", doi="10.1/x", title=ATTN, year=2017,
              authors=[{"family": "Vaswani", "given": "Ashish"}])
    res = await run({"title": ATTN, "year": 2017}, cache, crossref=FakeClient(metadata=[hit]))
    assert res["verified"] is True
    assert res["canonical"]["doi"] == "10.1/x"


ELDERLY = "Outcomes of robotic colectomy in elderly patients"
OLDER = "Outcomes of robotic colectomy in older patients"  # sim 0.8542, no subtitle relation


def test_r1_no_author_085_candidate_rejected():
    r = score_bibliographic_match({"title": ELDERLY, "year": 2020},
                                  {"title": OLDER, "year": 2020,
                                   "authors": [{"family": "Smith", "given": "J"}]})
    assert 0.84 < r["score_breakdown"]["title_sim"] < 0.86
    assert r["score_breakdown"]["rejected_by"] == "title_similarity_below_floor"
    assert r["match_quality"] == "none"


def test_r1_guard_with_author_085_candidate_as_today():
    """GUARD: with an input author, acceptance is exactly as today (0.70 floor, author guard)."""
    r = score_bibliographic_match({"title": ELDERLY, "authors": ["Smith J"], "year": 2020},
                                  {"title": OLDER, "year": 2020,
                                   "authors": [{"family": "Smith", "given": "J"}]})
    assert r["score_breakdown"]["rejected_by"] is None
    assert r["match_quality"] != "none"
    assert r["score_breakdown"]["weighted_score"] == pytest.approx(0.9271, abs=1e-4)  # v1.0.3 value


def test_r1_r2_subtitle_dropped_candidate_accepted_title_only():
    """Item-1 false-reject case: Crossref title lacks the subtitle (sim 0.661 < 0.70 today)."""
    full = "Recurrent secondary hyperparathyroidism: an argument for total parathyroidectomy"
    r = score_bibliographic_match({"title": full, "year": 2019},
                                  {"title": "Recurrent Secondary Hyperparathyroidism", "year": 2019})
    assert r["score_breakdown"]["rejected_by"] is None
    assert r["score_breakdown"]["title_sim"] == pytest.approx(0.661, abs=1e-3)  # R2: sim unchanged
    assert r["match_quality"] != "none"


# ---------------------------------------------------------------------------
# R2 — subtitle allowance word minimum
# ---------------------------------------------------------------------------


def test_r2_main_part_of_two_words_does_not_satisfy_allowance():
    """Sim 0.8085 is accepted today (>= 0.70); R1 rejects it and R2 cannot rescue a 2-word main part."""
    r = score_bibliographic_match({"title": "Robotic telesurgery: outcomes"},
                                  {"title": "Robotic Telesurgery"})
    assert r["score_breakdown"]["rejected_by"] == "title_similarity_below_floor"


def test_r2_main_part_of_three_words_satisfies_allowance():
    """Contrast to the 2-word case: a 3-word main part rescues a candidate that today falls
    below the 0.70 floor (long subtitle dropped)."""
    full = "Robotic remote telesurgery: a systematic review of long-term outcomes and costs"
    r = score_bibliographic_match({"title": full, "year": 2021},
                                  {"title": "Robotic Remote Telesurgery", "year": 2021})
    assert r["score_breakdown"]["title_sim"] < 0.70
    assert r["score_breakdown"]["rejected_by"] is None
    assert r["match_quality"] != "none"


# ---------------------------------------------------------------------------
# R3 / M1 — same-work merge
# ---------------------------------------------------------------------------

LOOKALIKE_PM = rec("pubmed", pmid="42927006", title="Attention when you need.", journal="ArXiv",
                   authors=[{"family": "Vaswani", "given": "A"}], year=2026,
                   abstract="Paying attention improves performance, but attention is metabolically costly")


async def test_r3_composite_across_two_dbs_pubmed_excluded(cache):
    """CM-105A 2b shape, with an input author so the PubMed look-alike passes its per-DB guards
    (first author matches, title_sim 0.75 >= 0.70) and only the R3 filter can catch it."""
    cr = rec("crossref", doi="10.65215/r5bs2d54", title=ATTN,
             authors=[{"family": "Vaswani", "given": "Ashish"}], year=2025)
    res = await run({"title": ATTN, "authors": ["Vaswani"]}, cache,
                    crossref=FakeClient(metadata=[cr]), pubmed=FakeClient(metadata=[LOOKALIKE_PM]))
    assert res["verified"] is True
    assert "pubmed" not in res["databases_confirmed"]
    assert "pubmed" not in res["canonical"]["sources"]
    assert res["canonical"]["pmid"] != "42927006"
    assert res["canonical"]["abstract"] is None
    assert res["canonical"]["journal"] != "ArXiv"
    assert not any(d["field"] == "title" for d in res["discrepancies"])
    # score describes the record that supplied canonical.title
    assert res["score_breakdown"]["title_sim"] == 1.0
    assert res["canonical"]["title"] == ATTN


async def test_r3_excluded_record_gives_exactly_one_warning_and_no_new_key(cache):
    cr = rec("crossref", doi="10.1/a", title=ATTN, authors=[{"family": "Vaswani", "given": "A"}])
    res = await run({"title": ATTN, "authors": ["Vaswani"]}, cache,
                    crossref=FakeClient(metadata=[cr]), pubmed=FakeClient(metadata=[LOOKALIKE_PM]))
    assert merge_warnings(res) == [{
        "source": "merge", "level": "warning",
        "message": "excluded_pubmed_title_disagrees_with_best_match",
    }]
    assert set(res) == TOP_KEYS
    assert set(res["canonical"]) == CANON_KEYS
    sb = set(res["score_breakdown"]) - {"capped_at_medium_insufficient_input_fields"}
    assert sb == L2_KEYS


async def test_r3_read_shape_canonical_not_from_crossref_lookalike(cache):
    """The 2026-11-16 READ: title + arxiv_id + year, no author. Crossref holds the LSD look-alike;
    OpenAlex, Semantic Scholar and arXiv hold the real paper."""
    real = dict(title=ATTN, year=2017, authors=[{"family": "Vaswani", "given": "Ashish"}])
    res = await run(
        {"title": ATTN, "arxiv_id": "1706.03762", "year": 2017}, cache,
        crossref=FakeClient(metadata=[LSD]),
        openalex=FakeClient(metadata=[rec("openalex", doi="10.48550/arxiv.1706.03762", **real)]),
        semantic_scholar=FakeClient(metadata=[rec("semantic_scholar", doi="10.48550/arxiv.1706.03762",
                                                  arxiv_id="1706.03762", **real)]),
        arxiv=FakeClient(arxiv=rec("arxiv", arxiv_id="1706.03762", **real)),
    )
    assert res["verified"] is True
    assert "crossref" not in res["databases_confirmed"]
    assert res["canonical"]["title"] == ATTN
    assert res["canonical"]["doi"] != "10.5040/9781350101272"
    assert res["canonical"]["authors"][0]["family"] == "Vaswani"


async def test_r3_guard_equal_titles_different_dois_both_kept(cache):
    """GUARD: preprint vs published (row_028 shape). Both kept; earliest year wins; the DOI
    difference stays an ordinary discrepancy and does not set requires_review."""
    t = "Deep Residual Learning for Image Recognition"
    a = [{"family": "He", "given": "Kaiming"}]
    res = await run(
        {"title": t, "authors": ["He"], "year": 2016}, cache,
        crossref=FakeClient(metadata=[rec("crossref", doi="10.1109/cvpr.2016.90", title=t, authors=a, year=2016)]),
        openalex=FakeClient(metadata=[rec("openalex", doi="10.48550/arxiv.1512.03385", title=t, authors=a, year=2015)]),
    )
    assert res["databases_confirmed"] == ["crossref", "openalex"]
    assert res["canonical"]["year"] == 2015
    assert any(d["field"] == "doi" for d in res["discrepancies"])
    assert res["requires_review"] is False
    assert merge_warnings(res) == []


async def test_r3_guard_markup_only_title_difference_not_excluded(cache):
    """GUARD: row_004 — Crossref title carries <scp> markup (0.9316 to the clean title), same DOI."""
    doi = "10.1111/bju.16742"
    cr_t = ("Robot‐assisted\n <scp>MRI</scp>\n /\n <scp>US</scp>\n fusion transperineal prostate biopsy "
            "using the Biobot system: a single‐centre experience")
    pm_t = "Robot-assisted MRI/US fusion transperineal prostate biopsy using the Biobot system: a single-centre experience."
    res = await run({"doi": doi}, cache,
                    crossref=FakeClient(doi=rec("crossref", doi=doi, title=cr_t)),
                    pubmed=FakeClient(doi=rec("pubmed", doi=doi, pmid="40254720", title=pm_t)))
    assert res["databases_confirmed"] == ["crossref", "pubmed"]
    assert res["canonical"]["pmid"] == "40254720"
    assert merge_warnings(res) == []


# ---------------------------------------------------------------------------
# R5 / S3 — tie-break
# ---------------------------------------------------------------------------


async def test_r5_identifier_layer_wins_equal_confidence_tie(cache):
    real = dict(title=ATTN, year=2017, authors=[{"family": "Vaswani", "given": "Ashish"}])
    res = await run(
        {"title": ATTN, "arxiv_id": "1706.03762", "year": 2017}, cache,
        crossref=FakeClient(metadata=[rec("crossref", doi="10.65215/x", **real)]),
        arxiv=FakeClient(arxiv=rec("arxiv", arxiv_id="1706.03762", **real)),
    )
    assert res["confidence"] == 1.0
    assert res["score_breakdown"] == {"layer": "identifier", "matched_identifier": "arxiv_id"}
    assert res["match_quality"] == "high"
    assert not any(w.get("source") == "scorer" for w in res["warnings"])


# ---------------------------------------------------------------------------
# R4 / S2 — cache key
# ---------------------------------------------------------------------------

BASE = {"title": "Some Title", "authors": ["Smith J", "Jones K"], "year": 2020, "journal": "Ann Surg",
        "volume": "271", "issue": "3", "pages": "1-9"}


@pytest.mark.parametrize("field,a,b", [
    ("pmid", "26982341", "32451395"),
    ("arxiv_id", "1706.03762", "1512.03385"),
    ("doi", "10.1/a", "10.1/b"),
    ("title", "Some Title", "Other Title"),
    ("authors", ["Smith J", "Jones K"], ["Smith J", "Brown K"]),
    ("year", 2020, 2021),
    ("journal", "Ann Surg", "Br J Surg"),
    ("volume", "271", "272"),
    ("issue", "3", "4"),
    ("pages", "1-9", "10-19"),
])
def test_r4_inputs_differing_in_one_field_get_different_keys(field, a, b):
    assert make_cache_key({**BASE, field: a}) != make_cache_key({**BASE, field: b})


def test_r4_pmid_only_inputs_get_different_keys():
    """The 2026-11-16 READ: pmid 15496622 then pmid 9500320 shared one cache entry."""
    assert make_cache_key({"pmid": "15496622"}) != make_cache_key({"pmid": "9500320"})


def test_r4_inputs_differing_only_in_arxiv_id_get_different_keys():
    t = {"title": ATTN, "year": 2017}
    assert make_cache_key({**t, "arxiv_id": "1706.03762"}) != make_cache_key(t)


def test_r4_doi_input_with_different_pmid_gets_different_key():
    assert make_cache_key({"doi": "10.1/a", "pmid": "1"}) != make_cache_key({"doi": "10.1/a", "pmid": "2"})


@pytest.mark.parametrize("a,b", [
    ({"doi": "https://doi.org/10.1056/NEJMoa2034577"}, {"doi": "10.1056/nejmoa2034577"}),
    ({"pmid": "0026982341"}, {"pmid": "26982341"}),
    ({"arxiv_id": "arXiv:1706.03762v5", "title": "T"}, {"arxiv_id": "1706.03762", "title": "T"}),
    ({"title": "The  Title!"}, {"title": "title"}),
])
def test_r4_guard_existing_normalisation_still_collapses(a, b):
    """GUARD: inputs equal after the existing normalisation keep one key."""
    assert make_cache_key(a) == make_cache_key(b)


# ---------------------------------------------------------------------------
# R6 / S1 — User-Agent
# ---------------------------------------------------------------------------


def _recording_transport(seen: list, body: bytes = b"{}", ctype="application/json"):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("user-agent"))
        return httpx.Response(200, content=body, headers={"Content-Type": ctype})
    return httpx.MockTransport(handler)


async def test_r6_user_agents_carry_package_version():
    want = f"citation-mcp/{citation_mcp.__version__}"
    seen: list = []
    cr = CrossrefClient(transport=_recording_transport(seen, b'{"message": {"items": []}}'))
    pm = PubMedClient(transport=_recording_transport(seen, b'{"esearchresult": {"idlist": []}}'))
    ax = ArxivClient(transport=_recording_transport(seen, b"<feed xmlns='http://www.w3.org/2005/Atom'/>",
                                                    "application/atom+xml"), min_interval=0.0)
    try:
        await cr.search_by_metadata(title="x")
        await pm.search_by_metadata(title="x")
        await ax.search_by_arxiv_id("1706.03762")
    finally:
        for c in (cr, pm, ax):
            await c.aclose()
    assert len(seen) == 3
    for ua in seen:
        assert ua.startswith(want + " ") or ua == want, ua
