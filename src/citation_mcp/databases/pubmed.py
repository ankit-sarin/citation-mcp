"""Async PubMed E-utilities client.

NCBI E-utilities surface:
  - esearch.fcgi  → returns a JSON list of PMIDs for a constructed query
  - efetch.fcgi   → returns full PubMedArticle XML records for a PMID list

We deliberately use XML for efetch because the JSON variant (retmode=json)
does not exist for the PubMed database — the documented path is XML, parsed
with the stdlib `xml.etree.ElementTree`.

Rate limit: 10 req/sec with an API key (3 req/sec without). The client
enforces both with a semaphore (concurrency cap) and a token-bucket pacer
(time-of-last-N-requests window).
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import random
import re
import time
from typing import Any
from xml.etree import ElementTree as ET

import httpx

from ..scoring import normalize_doi
from ..log_redaction import reraise_redacted

logger = logging.getLogger(__name__)

_BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_DEFAULT_TIMEOUT = 15.0
_TOOL_NAME = "citation-mcp"

_MAX_RETRIES = 3
_BASE_BACKOFF = 1.0
_BACKOFF_CAP = 10.0

_RATE_LIMIT_WITH_KEY = 10  # req per second
_RATE_LIMIT_WITHOUT_KEY = 3


def _polite_email() -> str:
    return os.environ.get("CROSSREF_POLITE_EMAIL", "asarin@ucdavis.edu")


def _api_key() -> str | None:
    return os.environ.get("NCBI_API_KEY") or None


class _RateLimiter:
    """Token-bucket pacer: ensures no more than N requests per rolling window.

    Combined with an outer semaphore that caps concurrency at N, this gives both
    the throughput cap and the per-second smoothing NCBI expects.
    """

    def __init__(self, max_per_second: int, window_seconds: float = 1.0) -> None:
        self._max = max_per_second
        self._window = window_seconds
        self._timestamps: collections.deque[float] = collections.deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            while self._timestamps and now - self._timestamps[0] >= self._window:
                self._timestamps.popleft()
            if len(self._timestamps) >= self._max:
                sleep_for = self._window - (now - self._timestamps[0])
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] >= self._window:
                    self._timestamps.popleft()
            self._timestamps.append(time.monotonic())


def _text(element: ET.Element | None) -> str | None:
    if element is None:
        return None
    text = "".join(element.itertext()).strip()
    return text or None


def _strip_translated_title_brackets(title: str | None) -> str | None:
    if not title:
        return title
    s = title.strip()
    if s.startswith("[") and s.endswith("].") and len(s) > 3:
        return s[1:-2].strip()
    if s.startswith("[") and s.endswith("]") and len(s) > 2:
        return s[1:-1].strip()
    return s


_MEDLINE_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def _parse_pubdate_year(pubdate: ET.Element | None) -> int | None:
    if pubdate is None:
        return None
    year_el = pubdate.find("Year")
    if year_el is not None and year_el.text:
        try:
            return int(year_el.text.strip())
        except ValueError:
            pass
    medline = pubdate.find("MedlineDate")
    if medline is not None and medline.text:
        m = _MEDLINE_YEAR_RE.search(medline.text)
        if m:
            return int(m.group(0))
    return None


def _parse_abstract(article: ET.Element) -> str | None:
    abstract_el = article.find("Abstract")
    if abstract_el is None:
        return None
    pieces: list[str] = []
    for at in abstract_el.findall("AbstractText"):
        text = "".join(at.itertext()).strip()
        if not text:
            continue
        label = at.get("Label") or at.get("NlmCategory")
        if label:
            pieces.append(f"{label}: {text}")
        else:
            pieces.append(text)
    if not pieces:
        return None
    return "\n".join(pieces)


def _parse_authors(article: ET.Element) -> list[dict]:
    out: list[dict] = []
    al = article.find("AuthorList")
    if al is None:
        return out
    for author in al.findall("Author"):
        last = author.findtext("LastName") or ""
        fore = author.findtext("ForeName") or author.findtext("Initials") or ""
        if not last and not fore:
            collective = author.findtext("CollectiveName")
            if collective:
                out.append({"family": collective.strip(), "given": ""})
            continue
        out.append({"family": last.strip(), "given": fore.strip()})
    return out


def _parse_pubmed_article(node: ET.Element) -> dict | None:
    """Parse a single <PubmedArticle> element into the normalized shape."""
    mc = node.find("MedlineCitation")
    if mc is None:
        return None
    pmid = mc.findtext("PMID")
    if pmid is None:
        return None
    pmid = pmid.strip()

    article = mc.find("Article")
    if article is None:
        return {
            "pmid": pmid,
            "doi": None,
            "title": None,
            "authors": [],
            "year": None,
            "journal": None,
            "journal_iso_abbrev": None,
            "volume": None,
            "issue": None,
            "pages": None,
            "abstract": None,
            "type": "journal-article",
            "source": "pubmed",
        }

    title = _text(article.find("ArticleTitle"))
    title = _strip_translated_title_brackets(title)

    journal_el = article.find("Journal")
    journal_full = _text(journal_el.find("Title")) if journal_el is not None else None
    journal_iso = _text(journal_el.find("ISOAbbreviation")) if journal_el is not None else None

    volume = None
    issue = None
    year = None
    if journal_el is not None:
        issue_el = journal_el.find("JournalIssue")
        if issue_el is not None:
            volume = _text(issue_el.find("Volume"))
            issue = _text(issue_el.find("Issue"))
            year = _parse_pubdate_year(issue_el.find("PubDate"))

    pagination = article.find("Pagination")
    pages = _text(pagination.find("MedlinePgn")) if pagination is not None else None

    abstract = _parse_abstract(article)
    authors = _parse_authors(article)

    pub_type = "journal-article"
    ptl = article.find("PublicationTypeList")
    if ptl is not None:
        first_pt = ptl.find("PublicationType")
        if first_pt is not None and first_pt.text:
            pub_type = first_pt.text.strip().lower().replace(" ", "-")

    doi: str | None = None
    pmdata = node.find("PubmedData")
    if pmdata is not None:
        ail = pmdata.find("ArticleIdList")
        if ail is not None:
            for aid in ail.findall("ArticleId"):
                if (aid.get("IdType") or "").lower() == "doi" and aid.text:
                    doi = normalize_doi(aid.text)
                    break

    return {
        "pmid": pmid,
        "doi": doi,
        "title": title,
        "authors": authors,
        "year": year,
        "journal": journal_full,
        "journal_iso_abbrev": journal_iso,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "abstract": abstract,
        "type": pub_type,
        "source": "pubmed",
    }


def _parse_pubmed_article_set(xml_bytes: bytes) -> list[dict]:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        logger.warning("PubMed XML parse failed: %s", e)
        return []
    if root.tag != "PubmedArticleSet":
        if root.tag == "PubmedArticle":
            parsed = _parse_pubmed_article(root)
            return [parsed] if parsed else []
        return []
    out: list[dict] = []
    for article in root.findall("PubmedArticle"):
        parsed = _parse_pubmed_article(article)
        if parsed is not None:
            out.append(parsed)
    return out


class PubMedClient:
    """Async NCBI E-utilities client (esearch + efetch)."""

    def __init__(
        self,
        timeout: float = _DEFAULT_TIMEOUT,
        base_url: str = _BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = _api_key()
        self._email = _polite_email()
        if self._api_key is None:
            logger.warning(
                "NCBI_API_KEY is not set; PubMed client will operate at 3 req/sec."
            )
            self._enabled = True  # PubMed allows anonymous use at lower rate
            rps = _RATE_LIMIT_WITHOUT_KEY
        else:
            self._enabled = True
            rps = _RATE_LIMIT_WITH_KEY
        self._semaphore = asyncio.Semaphore(rps)
        self._rate_limiter = _RateLimiter(rps)
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": f"citation-mcp/0.2.1 (mailto:{self._email})"},
        )

    def _params(self, **extra: Any) -> dict[str, Any]:
        p: dict[str, Any] = {"tool": _TOOL_NAME, "email": self._email}
        if self._api_key:
            p["api_key"] = self._api_key
        p.update({k: v for k, v in extra.items() if v is not None})
        return p

    async def _request(self, url: str, params: dict[str, Any]) -> httpx.Response:
        async with self._semaphore:
            await self._rate_limiter.acquire()
            for attempt in range(_MAX_RETRIES + 1):
                try:
                    response = await self._client.get(url, params=params)
                except (httpx.TransportError, httpx.TimeoutException):
                    if attempt >= _MAX_RETRIES:
                        raise
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    if attempt >= _MAX_RETRIES:
                        return response
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                return response
            raise RuntimeError("PubMed request failed after retries.")

    @staticmethod
    def _backoff(attempt: int) -> float:
        delay = min(_BASE_BACKOFF * (2 ** attempt), _BACKOFF_CAP)
        return delay + random.uniform(0, delay / 2)

    async def _esearch_pmids(self, term: str, retmax: int = 5) -> list[str]:
        response = await self._request(
            "/esearch.fcgi",
            self._params(db="pubmed", term=term, retmode="json", retmax=retmax),
        )
        if response.status_code != 200:
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as _err:
                reraise_redacted(_err)
        try:
            payload = response.json()
        except ValueError:
            return []
        idlist = (payload.get("esearchresult") or {}).get("idlist") or []
        return [str(x) for x in idlist if x]

    async def _efetch_pmids(self, pmids: list[str]) -> list[dict]:
        if not pmids:
            return []
        response = await self._request(
            "/efetch.fcgi",
            self._params(db="pubmed", id=",".join(pmids), retmode="xml"),
        )
        if response.status_code == 404:
            return []
        if response.status_code != 200:
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as _err:
                reraise_redacted(_err)
        return _parse_pubmed_article_set(response.content)

    async def search_by_pmid(self, pmid: str) -> dict | None:
        """efetch on PMID → normalized record, or None on miss."""
        pmid_s = str(pmid).strip().lstrip("0") or "0"
        if not pmid_s.isdigit():
            return None
        records = await self._efetch_pmids([pmid_s])
        return records[0] if records else None

    async def search_by_doi(self, doi: str) -> dict | None:
        """esearch by DOI → first PMID → search_by_pmid."""
        normalized = normalize_doi(doi)
        if not normalized:
            return None
        pmids = await self._esearch_pmids(f"{normalized}[doi]", retmax=1)
        if not pmids:
            return None
        return await self.search_by_pmid(pmids[0])

    async def search_by_metadata(
        self,
        title: str,
        author: str | None = None,
        year: int | None = None,
        journal: str | None = None,
        rows: int = 5,
    ) -> list[dict]:
        """Construct a fielded PubMed term, esearch, then efetch the PMIDs."""
        parts: list[str] = []
        if title:
            parts.append(f"{title}[Title]")
        if author:
            parts.append(f"{author}[Author]")
        if year is not None:
            parts.append(f"{year - 1}:{year + 1}[PDAT]")
        if journal:
            parts.append(f"{journal}[Journal]")
        if not parts:
            return []
        term = " AND ".join(parts)
        pmids = await self._esearch_pmids(term, retmax=rows)
        if not pmids:
            return []
        return await self._efetch_pmids(pmids)

    async def aclose(self) -> None:
        await self._client.aclose()
