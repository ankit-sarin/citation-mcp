"""Shared test fixtures: mocked HTTP clients for all five DB backends + cache."""

from __future__ import annotations

import json
from typing import Any, Callable

import httpx
import pytest_asyncio

from citation_mcp.cache import Cache
from citation_mcp.databases.arxiv import ArxivClient
from citation_mcp.databases.crossref import CrossrefClient
from citation_mcp.databases.openalex import OpenAlexClient
from citation_mcp.databases.pubmed import PubMedClient
from citation_mcp.databases.semantic_scholar import SemanticScholarClient

# ---------------------------------------------------------------------------
# Canonical Polack et al. 2020 NEJM record, per-DB raw shape
# ---------------------------------------------------------------------------

POLACK_DOI = "10.1056/nejmoa2034577"
POLACK_PMID = "33301246"
POLACK_OPENALEX_ID = "W3107657302"
POLACK_S2_ID = "fcabc9d5b3a2"
POLACK_TITLE = "Safety and Efficacy of the BNT162b2 mRNA Covid-19 Vaccine"

POLACK_RAW_CROSSREF: dict[str, Any] = {
    "DOI": "10.1056/NEJMoa2034577",
    "title": [POLACK_TITLE],
    "container-title": ["New England Journal of Medicine"],
    "author": [
        {"family": "Polack", "given": "Fernando P."},
        {"family": "Thomas", "given": "Stephen J."},
        {"family": "Kitchin", "given": "Nicholas"},
        {"family": "Absalon", "given": "Judith"},
        {"family": "Gurtman", "given": "Alejandra"},
    ],
    "published-print": {"date-parts": [[2020, 12, 31]]},
    "published-online": {"date-parts": [[2020, 12, 10]]},
    "created": {"date-parts": [[2020, 12, 10]]},
    "volume": "383",
    "issue": "27",
    "page": "2603-2615",
    "type": "journal-article",
}

POLACK_PUBMED_EFETCH_XML = f"""<?xml version="1.0" ?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID Version="1">{POLACK_PMID}</PMID>
      <Article>
        <Journal>
          <ISOAbbreviation>N Engl J Med</ISOAbbreviation>
          <Title>The New England journal of medicine</Title>
          <JournalIssue>
            <Volume>383</Volume>
            <Issue>27</Issue>
            <PubDate><Year>2020</Year><Month>Dec</Month></PubDate>
          </JournalIssue>
        </Journal>
        <ArticleTitle>{POLACK_TITLE}</ArticleTitle>
        <Pagination><MedlinePgn>2603-2615</MedlinePgn></Pagination>
        <Abstract>
          <AbstractText Label="BACKGROUND" NlmCategory="BACKGROUND">Severe acute respiratory syndrome coronavirus 2 (SARS-CoV-2) infection and the resulting coronavirus disease 2019 (Covid-19) have afflicted tens of millions of people.</AbstractText>
          <AbstractText Label="METHODS" NlmCategory="METHODS">In an ongoing multinational, placebo-controlled, observer-blinded trial, we randomly assigned persons 16 years of age or older.</AbstractText>
        </Abstract>
        <AuthorList>
          <Author><LastName>Polack</LastName><ForeName>Fernando P</ForeName></Author>
          <Author><LastName>Thomas</LastName><ForeName>Stephen J</ForeName></Author>
          <Author><LastName>Kitchin</LastName><ForeName>Nicholas</ForeName></Author>
        </AuthorList>
        <PublicationTypeList>
          <PublicationType>Journal Article</PublicationType>
        </PublicationTypeList>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">{POLACK_PMID}</ArticleId>
        <ArticleId IdType="doi">10.1056/NEJMoa2034577</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""

POLACK_PUBMED_ESEARCH_JSON: dict[str, Any] = {
    "esearchresult": {
        "count": "1",
        "retmax": "5",
        "retstart": "0",
        "idlist": [POLACK_PMID],
    },
}

POLACK_OPENALEX_JSON: dict[str, Any] = {
    "id": f"https://openalex.org/{POLACK_OPENALEX_ID}",
    "doi": f"https://doi.org/{POLACK_DOI}",
    "title": POLACK_TITLE,
    "display_name": POLACK_TITLE,
    "publication_year": 2020,
    "primary_location": {
        "source": {"display_name": "New England Journal of Medicine"},
    },
    "ids": {
        "openalex": f"https://openalex.org/{POLACK_OPENALEX_ID}",
        "doi": f"https://doi.org/{POLACK_DOI}",
        "pmid": f"https://pubmed.ncbi.nlm.nih.gov/{POLACK_PMID}",
    },
    "authorships": [
        {"author": {"display_name": "Fernando P. Polack"}},
        {"author": {"display_name": "Stephen J. Thomas"}},
        {"author": {"display_name": "Nicholas Kitchin"}},
    ],
    "biblio": {
        "volume": "383",
        "issue": "27",
        "first_page": "2603",
        "last_page": "2615",
    },
    "cited_by_count": 12000,
    "abstract_inverted_index": {
        "Severe": [0],
        "acute": [1],
        "respiratory": [2],
        "syndrome": [3],
        "coronavirus": [4, 6],
        "2": [5],
        "infection": [7],
    },
    "type": "journal-article",
}

POLACK_S2_JSON: dict[str, Any] = {
    "paperId": POLACK_S2_ID,
    "corpusId": 1234567,
    "externalIds": {
        "DOI": "10.1056/NEJMoa2034577",
        "PubMed": POLACK_PMID,
    },
    "title": POLACK_TITLE,
    "year": 2020,
    "authors": [
        {"name": "Fernando P. Polack"},
        {"name": "Stephen J. Thomas"},
    ],
    "venue": "New England Journal of Medicine",
    "publicationVenue": {"name": "New England Journal of Medicine"},
    "journal": {"name": "New England Journal of Medicine"},
    "abstract": "Severe acute respiratory syndrome coronavirus 2 infection.",
    "citationCount": 11800,
    "publicationTypes": ["JournalArticle"],
}

POLACK_ARXIV_FEED_EMPTY = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <title>ArXiv Query: search_query=</title>
  <opensearch:totalResults xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">0</opensearch:totalResults>
</feed>
"""

ARXIV_SAMPLE_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2103.04567v1</id>
    <published>2021-03-08T18:00:00Z</published>
    <title>Sample arXiv Paper About Transformers</title>
    <summary>This paper investigates transformer scaling.</summary>
    <author><name>Jane Q Doe</name></author>
    <author><name>John Smith</name></author>
    <arxiv:doi>10.9999/test.arxiv</arxiv:doi>
    <arxiv:journal_ref>Phys. Rev. D 99, 012345 (2021)</arxiv:journal_ref>
  </entry>
</feed>
"""


# ---------------------------------------------------------------------------
# Response envelope helpers (Crossref)
# ---------------------------------------------------------------------------


def works_response(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "ok",
        "message-type": "work",
        "message-version": "1.0.0",
        "message": item,
    }


def works_list_response(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "status": "ok",
        "message-type": "work-list",
        "message-version": "1.0.0",
        "message": {
            "total-results": len(items),
            "items-per-page": len(items),
            "items": items,
        },
    }


# ---------------------------------------------------------------------------
# Mock transport helpers
# ---------------------------------------------------------------------------


HandlerFn = Callable[[httpx.Request], httpx.Response]


def make_mock_crossref_client(handler: HandlerFn) -> CrossrefClient:
    return CrossrefClient(transport=httpx.MockTransport(handler))


def make_mock_pubmed_client(handler: HandlerFn) -> PubMedClient:
    return PubMedClient(transport=httpx.MockTransport(handler))


def make_mock_openalex_client(handler: HandlerFn) -> OpenAlexClient:
    return OpenAlexClient(transport=httpx.MockTransport(handler))


def make_mock_s2_client(handler: HandlerFn) -> SemanticScholarClient:
    return SemanticScholarClient(transport=httpx.MockTransport(handler))


def make_mock_arxiv_client(handler: HandlerFn, min_interval: float = 0.0) -> ArxivClient:
    return ArxivClient(transport=httpx.MockTransport(handler), min_interval=min_interval)


# ---------------------------------------------------------------------------
# Per-DB Polack-returning handlers
# ---------------------------------------------------------------------------


def polack_crossref_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/works":
        return httpx.Response(200, json=works_list_response([POLACK_RAW_CROSSREF]))
    if request.url.path.startswith("/works/"):
        return httpx.Response(200, json=works_response(POLACK_RAW_CROSSREF))
    return httpx.Response(404)


def polack_pubmed_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/esearch.fcgi"):
        return httpx.Response(200, json=POLACK_PUBMED_ESEARCH_JSON)
    if path.endswith("/efetch.fcgi"):
        return httpx.Response(200, content=POLACK_PUBMED_EFETCH_XML.encode("utf-8"),
                              headers={"Content-Type": "text/xml; charset=utf-8"})
    return httpx.Response(404)


def polack_openalex_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.startswith("/works/"):
        return httpx.Response(200, json=POLACK_OPENALEX_JSON)
    if path == "/works":
        return httpx.Response(200, json={"results": [POLACK_OPENALEX_JSON]})
    return httpx.Response(404)


def polack_s2_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if "/paper/search" in path:
        return httpx.Response(200, json={"data": [POLACK_S2_JSON]})
    if "/paper/" in path:
        return httpx.Response(200, json=POLACK_S2_JSON)
    return httpx.Response(404)


def polack_arxiv_handler(request: httpx.Request) -> httpx.Response:
    # arXiv has no record for an NEJM paper — return an empty Atom feed.
    return httpx.Response(200, content=POLACK_ARXIV_FEED_EMPTY,
                          headers={"Content-Type": "application/atom+xml"})


def sample_arxiv_handler(request: httpx.Request) -> httpx.Response:
    """Returns the canonical ARXIV_SAMPLE_FEED for any arXiv API request."""
    return httpx.Response(200, content=ARXIV_SAMPLE_FEED,
                          headers={"Content-Type": "application/atom+xml"})


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def cache(tmp_path):
    db_path = tmp_path / "test_cache.db"
    c = Cache(db_path=db_path)
    await c.init()
    try:
        yield c
    finally:
        await c.close()


@pytest_asyncio.fixture
async def polack_doi_client():
    """Crossref-only fixture (Phase 1.A compatibility)."""
    client = make_mock_crossref_client(polack_crossref_handler)
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def polack_metadata_client():
    client = make_mock_crossref_client(polack_crossref_handler)
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def polack_multi_db(monkeypatch):
    """Yields a dict of mocked clients for all 5 DBs returning Polack."""
    # Ensure OpenAlex is "enabled" in the test even when env var isn't set.
    monkeypatch.setenv("OPENALEX_API_KEY", "test-openalex-key")
    monkeypatch.setenv("NCBI_API_KEY", "test-ncbi-key")
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "test-s2-key")

    clients = {
        "crossref": make_mock_crossref_client(polack_crossref_handler),
        "pubmed": make_mock_pubmed_client(polack_pubmed_handler),
        "openalex": make_mock_openalex_client(polack_openalex_handler),
        "semantic_scholar": make_mock_s2_client(polack_s2_handler),
        "arxiv": make_mock_arxiv_client(polack_arxiv_handler, min_interval=0.0),
    }
    try:
        yield clients
    finally:
        for c in clients.values():
            await c.aclose()
