"""
OpenAlex API client - every HTTP call and response parse lives here.

Same adapter split as the weather projects: this module talks to the API and
returns plain dicts shaped like the Lakebase tables; nothing else in the app
knows what an OpenAlex response looks like.

Costs, measured against the live API (X-RateLimit-Credits-Used header):

    GET /works/{id}                     0 credits
    GET /works?filter=...  (list/search) 10 credits
    GET content.openalex.org/...        100 credits, and REQUIRES an API key

Without a key the budget is 1,000 credits/day - 100 searches. A free key raises
it to 100,000. The client reports the last call's usage so /copilot/stats can
show how much of the day is left.
"""

from __future__ import annotations

import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from typing import Any

import requests

logger = logging.getLogger(__name__)

API_BASE = os.environ.get("OPENALEX_API_BASE", "https://api.openalex.org")
CONTENT_BASE = os.environ.get("OPENALEX_CONTENT_BASE", "https://content.openalex.org")
HTTP_TIMEOUT = float(os.environ.get("OPENALEX_HTTP_TIMEOUT", "20"))

MAX_PER_PAGE = 50
# Full text can run to hundreds of thousands of characters. Beyond this the
# chunk count - and the embed time - grows with no retrieval benefit for a
# study plan, so the tail is dropped and the paper is marked truncated.
MAX_CONTENT_CHARS = int(os.environ.get("OPENALEX_MAX_CONTENT_CHARS", "60000"))

# Only the fields the app stores. Selecting keeps list responses small; the
# credit cost is the same either way.
SELECT_FIELDS = (
    "id,doi,display_name,publication_year,publication_date,type,language,"
    "cited_by_count,open_access,primary_location,primary_topic,topics,"
    "abstract_inverted_index,authorships,referenced_works,related_works,"
    "has_content,is_retracted,is_paratext"
)

_WORK_ID = re.compile(r"^W\d+$")
_SURVEY_HINT = re.compile(r"\b(survey|review|overview|tutorial|primer)\b", re.I)


class OpenAlexError(Exception):
    """A failure worth showing to a person (or an agent) as-is."""


def short_id(value: str | None) -> str | None:
    """'https://openalex.org/W4389984066' -> 'W4389984066'."""
    if not value:
        return None
    return value.rstrip("/").rsplit("/", 1)[-1]


def normalize_work_id(value: str) -> str:
    """Accept a bare id, an openalex.org URL, or a lowercase id; reject the rest."""
    candidate = (short_id(value.strip()) or "").upper()
    if not _WORK_ID.match(candidate):
        raise OpenAlexError(
            f"{value!r} is not an OpenAlex work id. Expected something like 'W4389984066'."
        )
    return candidate


def reconstruct_abstract(inverted_index: dict[str, list[int]] | None) -> str | None:
    """Rebuild abstract text from OpenAlex's inverted index.

    OpenAlex cannot redistribute abstracts verbatim, so it publishes
    {"word": [positions...]}. Placing each word at each of its positions and
    joining restores the original order.
    """
    if not inverted_index:
        return None
    positions: dict[int, str] = {}
    for word, indexes in inverted_index.items():
        for index in indexes:
            positions[index] = word
    if not positions:
        return None
    text = " ".join(positions[i] for i in sorted(positions))
    return text.strip() or None


def tei_to_text(xml_text: str, max_chars: int = MAX_CONTENT_CHARS) -> tuple[str, bool]:
    """Extract readable body text from a GROBID TEI document.

    Returns (text, truncated). Section heads are kept on their own line because
    they make chunks self-describing ("3 Method ..."). Bibliography, figures and
    tables are skipped: they embed as noise and crowd out real prose.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as err:
        raise OpenAlexError(f"Full text could not be parsed as TEI XML: {err}") from err

    ns = {"tei": "http://www.tei-c.org/ns/1.0"}
    body = root.find(".//tei:text/tei:body", ns)
    if body is None:
        return "", False

    parts: list[str] = []
    for div in body.findall(".//tei:div", ns):
        head = div.find("tei:head", ns)
        if head is not None:
            title = " ".join("".join(head.itertext()).split())
            if title:
                parts.append(title)
        for para in div.findall("tei:p", ns):
            text = " ".join("".join(para.itertext()).split())
            if text:
                parts.append(text)

    text = "\n".join(parts)
    if len(text) > max_chars:
        return text[:max_chars], True
    return text, False


def looks_like_survey(title: str | None, work_type: str | None) -> bool:
    return work_type == "review" or bool(_SURVEY_HINT.search(title or ""))


def normalize_work(raw: dict[str, Any]) -> dict[str, Any]:
    """Shape one OpenAlex work into {"paper", "authors", "authorships"}.

    The three keys map one-to-one onto the papers, authors and paper_authors
    tables, so the store can upsert them without reinterpreting anything.
    """
    paper_id = short_id(raw.get("id"))
    if not paper_id:
        raise OpenAlexError("OpenAlex returned a work without an id.")

    location = raw.get("primary_location") or {}
    source = location.get("source") or {}
    open_access = raw.get("open_access") or {}
    primary_topic = raw.get("primary_topic") or {}
    has_content = raw.get("has_content") or {}

    # The raw payload is kept for provenance, minus the inverted index: it is
    # the bulkiest field and the abstract column already holds its content.
    payload = {k: v for k, v in raw.items() if k != "abstract_inverted_index"}

    paper = {
        "id": paper_id,
        "doi": raw.get("doi"),
        "title": raw.get("display_name") or "(untitled)",
        "abstract": reconstruct_abstract(raw.get("abstract_inverted_index")),
        "publication_year": raw.get("publication_year"),
        "publication_date": raw.get("publication_date"),
        "venue": source.get("display_name"),
        "work_type": raw.get("type"),
        "language": raw.get("language"),
        "cited_by_count": raw.get("cited_by_count") or 0,
        "is_oa": bool(open_access.get("is_oa")),
        "oa_url": open_access.get("oa_url"),
        "primary_topic": primary_topic.get("display_name"),
        "topics": [
            {"name": t.get("display_name"), "score": round(float(t.get("score") or 0), 3)}
            for t in (raw.get("topics") or [])[:5]
        ],
        "referenced_works": [s for s in map(short_id, raw.get("referenced_works") or []) if s],
        "related_works": [s for s in map(short_id, raw.get("related_works") or []) if s],
        "has_content": bool(has_content.get("grobid_xml")),
        "payload": payload,
    }

    authors, authorships = [], []
    seen: set[str] = set()
    for index, authorship in enumerate(raw.get("authorships") or []):
        author = authorship.get("author") or {}
        author_id = short_id(author.get("id"))
        # OpenAlex occasionally lists the same author twice on one byline; the
        # (paper_id, author_id) primary key would reject the second row.
        if not author_id or author_id in seen:
            continue
        seen.add(author_id)
        institutions = [
            {
                "id": short_id(inst.get("id")),
                "name": inst.get("display_name"),
                "country": inst.get("country_code"),
            }
            for inst in authorship.get("institutions") or []
        ]
        authors.append({
            "id": author_id,
            "display_name": author.get("display_name") or authorship.get("raw_author_name") or "Unknown",
            "orcid": author.get("orcid"),
            "institutions": institutions,
        })
        authorships.append({
            "paper_id": paper_id,
            "author_id": author_id,
            "author_position": authorship.get("author_position"),
            "position_index": index,
            "is_corresponding": bool(authorship.get("is_corresponding")),
            "institutions": institutions,
        })

    return {"paper": paper, "authors": authors, "authorships": authorships}


class OpenAlexClient:
    """Thin requests wrapper with key handling, one 429 retry and usage tracking."""

    def __init__(self, api_key: str | None = None, mailto: str | None = None,
                 timeout: float = HTTP_TIMEOUT):
        self.api_key = api_key if api_key is not None else os.environ.get("OPENALEX_API_KEY") or None
        self.mailto = mailto if mailto is not None else os.environ.get("OPENALEX_MAILTO") or None
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = (
            f"research-learning-copilot ({self.mailto or 'no contact configured'})"
        )
        self.last_usage: dict[str, Any] = {}

    # -- transport -----------------------------------------------------------

    def _get(self, url: str, params: dict | None = None, *, expect_json: bool = True):
        params = dict(params or {})
        if self.api_key:
            params["api_key"] = self.api_key
        if self.mailto:
            params["mailto"] = self.mailto

        for attempt in range(2):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as err:
                raise OpenAlexError(f"OpenAlex could not be reached: {type(err).__name__}") from err

            self._record_usage(response)
            if response.status_code == 429 and attempt == 0:
                wait = min(float(response.headers.get("Retry-After") or 2), 10.0)
                logger.warning("OpenAlex rate limited; retrying in %.1fs", wait)
                time.sleep(wait)
                continue
            break

        if response.status_code == 401:
            raise OpenAlexError(
                "OpenAlex rejected the request as unauthorized. Full-text content needs "
                "OPENALEX_API_KEY; if one is set, check that it is valid."
            )
        if response.status_code == 404:
            raise OpenAlexError("OpenAlex has no record at that id.")
        if response.status_code == 429:
            raise OpenAlexError(
                "The OpenAlex daily credit budget is used up. Add OPENALEX_API_KEY for a "
                "larger allowance, or try again after the reset."
            )
        if response.status_code >= 400:
            raise OpenAlexError(f"OpenAlex returned HTTP {response.status_code}.")
        return response.json() if expect_json else response.text

    def _record_usage(self, response) -> None:
        headers = response.headers
        self.last_usage = {
            "credits_used": _int_or_none(headers.get("X-RateLimit-Credits-Used")),
            "credits_remaining": _int_or_none(headers.get("X-RateLimit-Remaining")),
            "credits_limit": _int_or_none(headers.get("X-RateLimit-Limit")),
            "reset_seconds": _int_or_none(headers.get("X-RateLimit-Reset")),
            "authenticated": bool(self.api_key),
        }

    # -- works ---------------------------------------------------------------

    def search_works(
        self,
        query: str,
        *,
        limit: int = 10,
        from_year: int | None = None,
        to_year: int | None = None,
        open_access_only: bool = False,
        min_citations: int | None = None,
        sort: str = "relevance",
    ) -> list[dict[str, Any]]:
        """Title-and-abstract search, normalized. One call, 10 credits.

        title_and_abstract.search rather than the bare `search` parameter: the
        live API expands `search` to a full-text match, which surfaces papers
        that merely mention a phrase in passing. A learning goal wants papers
        that are *about* it.
        """
        query = " ".join((query or "").split())
        if not query:
            raise OpenAlexError("A search needs a non-empty query.")
        # Commas separate filters, so one inside the query would split it.
        filters = [f"title_and_abstract.search:{query.replace(',', ' ')}",
                   "is_paratext:false", "is_retracted:false"]
        if from_year:
            filters.append(f"from_publication_date:{int(from_year)}-01-01")
        if to_year:
            filters.append(f"to_publication_date:{int(to_year)}-12-31")
        if open_access_only:
            filters.append("is_oa:true")
        if min_citations:
            filters.append(f"cited_by_count:>{max(0, int(min_citations) - 1)}")

        if sort not in ("relevance", "citations", "recent"):
            raise OpenAlexError("sort must be one of: relevance, citations, recent.")
        limit = max(1, min(int(limit), MAX_PER_PAGE))

        # Always rank by relevance upstream. Measured on "retrieval augmented
        # generation": sort=cited_by_count:desc put a 2020 cognitive-science
        # preprint and a 2016 walking-route paper in the top two - any record
        # that loosely matches and is highly cited floats up, quoted phrase or
        # not. So "citations" and "recent" re-sort a relevance-ranked pool
        # instead. Same single call, same 10 credits.
        pool = limit if sort == "relevance" else min(MAX_PER_PAGE, limit * 3)
        data = self._get(f"{API_BASE}/works", {
            "filter": ",".join(filters),
            "sort": "relevance_score:desc",
            "per_page": pool,
            "select": SELECT_FIELDS,
        })
        works = [normalize_work(raw) for raw in data.get("results") or []]
        if sort == "citations":
            works.sort(key=lambda w: -(w["paper"]["cited_by_count"] or 0))
        elif sort == "recent":
            works.sort(key=lambda w: w["paper"]["publication_date"] or "", reverse=True)
        return works[:limit]

    def get_work(self, work_id: str) -> dict[str, Any]:
        """One work by id, normalized. Free."""
        data = self._get(f"{API_BASE}/works/{normalize_work_id(work_id)}",
                         {"select": SELECT_FIELDS})
        return normalize_work(data)

    def fetch_content_text(self, work_id: str) -> tuple[str, bool] | None:
        """Open-access full text as plain text, or None when there is none.

        100 credits per call and key-only, so callers decide when it is worth it
        (the importer only does it for papers a learner saves, never for search
        results).
        """
        if not self.api_key:
            return None
        work_id = normalize_work_id(work_id)
        xml_text = self._get(f"{CONTENT_BASE}/works/{work_id}.grobid-xml", expect_json=False)
        text, truncated = tei_to_text(xml_text)
        return (text, truncated) if text else None


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
