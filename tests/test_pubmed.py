"""Tests for the PubMed E-utilities client."""

from __future__ import annotations

import httpx
import pytest_asyncio

from citation_mcp.databases.pubmed import (
    PubMedClient,
    _parse_pubmed_article_set,
)

from .conftest import (
    POLACK_PMID,
    POLACK_PUBMED_EFETCH_XML,
    POLACK_PUBMED_ESEARCH_JSON,
    POLACK_TITLE,
    make_mock_pubmed_client,
    polack_pubmed_handler,
)


@pytest_asyncio.fixture
async def pubmed_client():
    client = make_mock_pubmed_client(polack_pubmed_handler)
    try:
        yield client
    finally:
        await client.aclose()


async def test_search_by_pmid_returns_normalized_record(pubmed_client: PubMedClient):
    rec = await pubmed_client.search_by_pmid(POLACK_PMID)
    assert rec is not None
    assert rec["pmid"] == POLACK_PMID
    assert rec["doi"] == "10.1056/nejmoa2034577"
    assert rec["title"] == POLACK_TITLE
    assert rec["year"] == 2020
    assert rec["journal"] == "The New England journal of medicine"
    assert rec["journal_iso_abbrev"] == "N Engl J Med"
    assert rec["volume"] == "383"
    assert rec["issue"] == "27"
    assert rec["pages"] == "2603-2615"
    assert rec["source"] == "pubmed"
    assert rec["authors"][0] == {"family": "Polack", "given": "Fernando P"}


async def test_search_by_doi_via_esearch(pubmed_client: PubMedClient):
    rec = await pubmed_client.search_by_doi("10.1056/NEJMoa2034577")
    assert rec is not None
    assert rec["pmid"] == POLACK_PMID


async def test_search_by_metadata(pubmed_client: PubMedClient):
    results = await pubmed_client.search_by_metadata(
        title=POLACK_TITLE,
        author="Polack",
        year=2020,
        rows=5,
    )
    assert len(results) == 1
    assert results[0]["pmid"] == POLACK_PMID


async def test_structured_abstract_parsing(pubmed_client: PubMedClient):
    rec = await pubmed_client.search_by_pmid(POLACK_PMID)
    assert rec is not None
    # Structured abstract should preserve labels with prefix.
    assert "BACKGROUND:" in rec["abstract"]
    assert "METHODS:" in rec["abstract"]


async def test_empty_efetch_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/efetch.fcgi"):
            return httpx.Response(200, content=b"<PubmedArticleSet/>",
                                  headers={"Content-Type": "text/xml"})
        return httpx.Response(404)
    client = make_mock_pubmed_client(handler)
    try:
        rec = await client.search_by_pmid("99999999")
        assert rec is None
    finally:
        await client.aclose()


def test_parse_pubmed_article_set_handles_multiple_articles():
    xml = b"""<PubmedArticleSet>
      <PubmedArticle>
        <MedlineCitation>
          <PMID>1</PMID>
          <Article><ArticleTitle>One</ArticleTitle></Article>
        </MedlineCitation>
      </PubmedArticle>
      <PubmedArticle>
        <MedlineCitation>
          <PMID>2</PMID>
          <Article><ArticleTitle>Two</ArticleTitle></Article>
        </MedlineCitation>
      </PubmedArticle>
    </PubmedArticleSet>"""
    records = _parse_pubmed_article_set(xml)
    assert len(records) == 2
    assert records[0]["pmid"] == "1"
    assert records[1]["pmid"] == "2"


def test_translated_title_brackets_stripped():
    xml = b"""<PubmedArticleSet>
      <PubmedArticle>
        <MedlineCitation>
          <PMID>3</PMID>
          <Article><ArticleTitle>[Translated title here].</ArticleTitle></Article>
        </MedlineCitation>
      </PubmedArticle>
    </PubmedArticleSet>"""
    records = _parse_pubmed_article_set(xml)
    assert records[0]["title"] == "Translated title here"
