"""HTTP adapters for bounded scholarly metadata retrieval."""

from __future__ import annotations

import time
from http import HTTPStatus
from typing import Any, Callable
from urllib.parse import quote_plus
from xml.etree import ElementTree as etree

import httpx


RETRIEVAL_RESULTS_PER_QUERY = 20


def arxiv_search(
    query: str,
    client: httpx.Client,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, str]]:
    url = (
        "https://export.arxiv.org/api/query?search_query=all:"
        + quote_plus(query)
        + f"&start=0&max_results={RETRIEVAL_RESULTS_PER_QUERY}"
    )
    response: httpx.Response | None = None
    for attempt in range(3):
        response = client.get(
            url,
            headers={"User-Agent": "EvidenceReviewerDemo/0.1 (research prototype)"},
        )
        if response.status_code != HTTPStatus.TOO_MANY_REQUESTS:
            break
        sleep(2 * (attempt + 1))
    if response is None:
        return []
    response.raise_for_status()
    root = etree.fromstring(response.text)
    namespace = {"atom": "http://www.w3.org/2005/Atom"}
    records: list[dict[str, str]] = []
    for entry in root.findall("atom:entry", namespace):
        title = " ".join(
            entry.findtext("atom:title", default="", namespaces=namespace).split()
        )
        summary = " ".join(
            entry.findtext("atom:summary", default="", namespaces=namespace).split()
        )
        published = entry.findtext(
            "atom:published", default="", namespaces=namespace
        )[:10]
        identifier = entry.findtext("atom:id", default="", namespaces=namespace)
        authors = [
            author.findtext("atom:name", default="", namespaces=namespace)
            for author in entry.findall("atom:author", namespace)
        ]
        if title:
            records.append(
                {
                    "title": title,
                    "authors": ", ".join(author for author in authors if author),
                    "published": published,
                    "url": identifier,
                    "summary": summary[:1100],
                    "query": query,
                    "source": "arXiv API",
                }
            )
    return records


def openalex_abstract(record: dict[str, Any]) -> str:
    positions = record.get("abstract_inverted_index") or {}
    if not isinstance(positions, dict):
        return ""
    words: list[tuple[int, str]] = []
    for word, offsets in positions.items():
        if isinstance(offsets, list):
            words.extend(
                (int(offset), str(word))
                for offset in offsets
                if isinstance(offset, int)
            )
    return " ".join(word for _, word in sorted(words))


def openalex_search(
    query: str,
    client: httpx.Client,
    *,
    review_as_of: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, str]]:
    params: dict[str, str | int] = {
        "search": query,
        "per-page": RETRIEVAL_RESULTS_PER_QUERY,
    }
    if review_as_of:
        params["filter"] = (
            f"from_publication_date:1900-01-01,to_publication_date:{review_as_of}"
        )
    response: httpx.Response | None = None
    for attempt in range(3):
        response = client.get(
            "https://api.openalex.org/works",
            params=params,
            headers={"User-Agent": "EvidenceReviewerDemo/0.1 (research prototype)"},
        )
        if response.status_code not in {
            HTTPStatus.TOO_MANY_REQUESTS,
            HTTPStatus.SERVICE_UNAVAILABLE,
        }:
            break
        sleep(1.5 * (attempt + 1))
    if response is None:
        return []
    response.raise_for_status()
    payload = response.json()
    records: list[dict[str, str]] = []
    for item in payload.get("results", []):
        if not isinstance(item, dict) or not item.get("display_name"):
            continue
        authors = []
        for authorship in item.get("authorships") or []:
            author = authorship.get("author") if isinstance(authorship, dict) else {}
            if isinstance(author, dict) and author.get("display_name"):
                authors.append(str(author["display_name"]))
        location = item.get("primary_location") or {}
        source_url = (
            location.get("landing_page_url") if isinstance(location, dict) else None
        )
        records.append(
            {
                "title": str(item["display_name"]),
                "authors": ", ".join(authors),
                "published": str(item.get("publication_date") or ""),
                "url": str(source_url or item.get("doi") or item.get("id") or ""),
                "summary": openalex_abstract(item)[:1100],
                "query": query,
                "source": "OpenAlex fallback",
            }
        )
    return records


__all__ = ["arxiv_search", "openalex_abstract", "openalex_search"]
