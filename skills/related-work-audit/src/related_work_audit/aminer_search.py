"""AMiner Open Platform search and safe manuscript identity resolution.

The Skill owns this small client so it stays installable without the AMiner
research-skills repository.  Only documented Open Platform endpoints are used.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from datetime import date
from difflib import SequenceMatcher
from typing import Any, Callable

import httpx

from .aminer import normalize_pub_id
from .identity import aminer_headers
from .publication_time import arxiv_month_evidence, date_bounds, earliest_supported_time


AMINER_OPEN_PLATFORM_BASE = (
    "https://datacenter.aminer.cn/gateway/open_platform"
)
AMINER_PAPER_SEARCH_URL = f"{AMINER_OPEN_PLATFORM_BASE}/api/paper/search"
AMINER_PAPER_SEARCH_PRO_URL = (
    f"{AMINER_OPEN_PLATFORM_BASE}/api/paper/search/pro"
)
AMINER_PAPER_QA_SEARCH_PRO_URL = (
    f"{AMINER_OPEN_PLATFORM_BASE}/api/paper/qa/searchPro"
)
AMINER_PAPER_QA_SEARCH_URL = (
    f"{AMINER_OPEN_PLATFORM_BASE}/api/paper/qa/search"
)
AMINER_PAPER_INFO_URL = f"{AMINER_OPEN_PLATFORM_BASE}/api/paper/info"
AMINER_PAPER_DETAIL_URL = f"{AMINER_OPEN_PLATFORM_BASE}/api/paper/detail"
AMINER_PAPER_LIST_BY_KEYWORDS_URL = (
    f"{AMINER_OPEN_PLATFORM_BASE}/api/paper/list/citation/by/keywords"
)

CONTROLLED_MECHANISM_DOMAIN = "controlled_mechanism_domain"
AMINER_API_PRICES_YUAN = {
    "paper_search": 0.0,
    "paper_search_pro": 0.01,
    "paper_qa_search": 0.05,
    "paper_qa_search_pro": 0.30,
    "paper_info": 0.0,
    "paper_detail": 0.01,
    "paper_list_by_keywords": 0.10,
}


class AMinerAPIError(ValueError):
    """Sanitized AMiner failure carrying only operational response metadata."""

    def __init__(self, message: str, diagnostic: dict[str, Any]):
        super().__init__(message)
        self.diagnostic = diagnostic


def _response_data(response: httpx.Response, label: str) -> Any:
    try:
        payload = response.json()
    except ValueError as exc:
        diagnostic = {"http_status": response.status_code}
        response.extensions["aminer_api_diagnostic"] = diagnostic
        raise AMinerAPIError(
            f"AMiner {label} returned invalid JSON.", diagnostic
        ) from exc
    diagnostic: dict[str, Any] = {"http_status": response.status_code}
    if isinstance(payload, dict):
        for key in ("code", "success", "log_id", "total"):
            if payload.get(key) is not None:
                diagnostic[key] = payload.get(key)
        message = payload.get("message") or payload.get("msg")
        if message is not None:
            diagnostic["msg"] = str(message)[:240]
    response.extensions["aminer_api_diagnostic"] = diagnostic
    if response.is_error:
        raise AMinerAPIError(
            f"AMiner {label} returned HTTP {response.status_code}.", diagnostic
        )
    if not isinstance(payload, dict):
        return payload
    code = payload.get("code")
    success = payload.get("success")
    if success is False or (code is not None and code not in {0, 200, "0", "200"}):
        message = str(payload.get("message") or payload.get("msg") or "API failure")
        raise AMinerAPIError(
            f"AMiner {label} failed with code {code!s}: {message}", diagnostic
        )
    return payload.get("data", payload)


def _rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return []
    for key in ("items", "records", "list", "results", "data"):
        nested = value.get(key)
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
    # paper/relation and some info responses wrap a single record.
    if any(key in value for key in ("id", "_id", "paper_id", "title")):
        return [value]
    return []


def _paper_id(row: dict[str, Any]) -> str:
    raw = str(row.get("id") or row.get("_id") or row.get("paper_id") or "")
    try:
        return normalize_pub_id(raw)
    except ValueError:
        return ""


def _title(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _published_value(row: dict[str, Any]) -> str:
    return _choose_publication_evidence(_publication_evidence(row, "provider"))


def _publication_evidence(row: dict[str, Any], source: str) -> list[dict[str, str]]:
    """Retain provider-reported publication fields with honest precision."""
    evidence = []
    for key in (
        "publication_date",
        "publicationDate",
        "published",
        "published_at",
        "date",
    ):
        value = _title(row.get(key))
        if re.fullmatch(r"\d{4}-\d{2}", value) and date_bounds(value):
            evidence.append({"source": source, "field": key, "value": value, "precision": "month"})
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ].*)?", value):
            try:
                date.fromisoformat(value[:10])
            except ValueError:
                continue
            evidence.append({"source": source, "field": key, "value": value[:10], "precision": "day"})
    year = str(row.get("year") or "").strip()
    if re.fullmatch(r"\d{4}", year) and 1 <= int(year) <= 9999:
        evidence.append({"source": source, "field": "year", "value": year, "precision": "year"})
    evidence.extend(arxiv_month_evidence(row, source))
    return evidence


def _choose_publication_evidence(evidence: list[dict[str, str]]) -> str:
    # An earlier supported availability window survives later venue metadata.
    # These are provider claims, not independently verified first-public dates.
    return earliest_supported_time(evidence)


def _abstract_value(row: dict[str, Any]) -> str:
    return max((
        _title(row.get(key)) for key in
        ("abstract", "abstract_zh", "abstract_slice", "abstract_zh_slice")
        if isinstance(row.get(key), str)
    ), key=len, default="")[:10000]


def normalize_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return " ".join(normalized.split())


def _author_names(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    rendered: list[str] = []
    for item in value:
        if isinstance(item, dict):
            item = item.get("name") or item.get("name_zh")
        name = _title(item)
        if name:
            rendered.append(name)
    return rendered


def _author_keys(names: list[str]) -> set[str]:
    keys: set[str] = set()
    for name in names:
        tokens = re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKC", name).casefold())
        if not tokens:
            continue
        compact = "".join(tokens)
        if len(compact) >= 4:
            keys.add(compact)
        # Surnames are useful across "Given Family" / "Family, Given" variants.
        for token in (tokens[0], tokens[-1]):
            if len(token) >= 3:
                keys.add(token)
    return keys


def _record_call(
    calls: list[dict[str, Any]],
    api: str,
    *,
    status: str,
    query: str | None = None,
    query_shape: str | None = None,
    query_subtype: str | None = None,
    mechanism_branch: str | None = None,
    feedback_origin: str | None = None,
    request_method: str | None = None,
    request_parameters: dict[str, Any] | None = None,
    route_reason: str | None = None,
    detail: str | None = None,
    response_metadata: dict[str, Any] | None = None,
) -> None:
    call = {
        "api": api,
        "endpoint": {
            "paper_search": "/api/paper/search",
            "paper_search_pro": "/api/paper/search/pro",
            "paper_qa_search": "/api/paper/qa/search",
            "paper_qa_search_pro": "/api/paper/qa/searchPro",
            "paper_info": "/api/paper/info",
            "paper_detail": "/api/paper/detail",
            "paper_list_by_keywords": "/api/paper/list/citation/by/keywords",
        }[api],
        "unit_price_yuan": AMINER_API_PRICES_YUAN[api],
        "estimated_fee_yuan": AMINER_API_PRICES_YUAN[api],
        "status": status,
    }
    if query:
        call["query"] = query
    if query_shape:
        call["query_shape"] = query_shape
    if query_subtype:
        call["query_subtype"] = query_subtype
    if mechanism_branch:
        call["mechanism_branch"] = mechanism_branch
    if feedback_origin:
        call["feedback_origin"] = feedback_origin
    if request_method:
        call["request_method"] = request_method
    if request_parameters is not None:
        call["request"] = request_parameters
        call["parameter_shape"] = {
            key: type(value).__name__ for key, value in request_parameters.items()
        }
    if route_reason:
        call["route_reason"] = route_reason
    if detail:
        call["detail"] = detail[:240]
    if response_metadata:
        call["response"] = {
            key: response_metadata[key]
            for key in ("http_status", "code", "success", "msg", "log_id", "total")
            if response_metadata.get(key) is not None
        }
    calls.append(call)


def _cost_summary(calls: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(str(call.get("api") or "unknown") for call in calls)
    breakdown = [
        {
            "api": api,
            "calls": count,
            "unit_price_yuan": AMINER_API_PRICES_YUAN.get(api),
            "estimated_subtotal_yuan": round(
                count * float(AMINER_API_PRICES_YUAN.get(api) or 0.0), 2
            ),
        }
        for api, count in sorted(counts.items())
    ]
    return {
        "currency": "CNY",
        "estimated_total_yuan": round(
            sum(
                float(AMINER_API_PRICES_YUAN.get(str(call.get("api"))) or 0.0)
                for call in calls
            ),
            2,
        ),
        "calls": len(calls),
        "breakdown": breakdown,
        "note": "Estimated from the official AMiner Open Platform catalog; gateway billing is authoritative.",
    }


def resolve_title_to_pub_id(
    title: str,
    *,
    authors: list[str] | None,
    manuscript_year: int | None,
    review_as_of: str,
    api_key: str,
    proxy: str | None,
    client_factory: Callable[..., Any] = httpx.Client,
) -> dict[str, Any]:
    """Resolve a PDF title only when title plus author/year evidence is unique.

    A unique title alone is intentionally insufficient.  Blind submissions often
    omit authors, so those runs report ``insufficient_identity_evidence`` instead
    of attaching another paper's citation graph.
    """
    clean_title = _title(title)
    local_authors = [_title(name) for name in (authors or []) if _title(name)]
    cutoff = date.fromisoformat(review_as_of)
    calls: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "provider": "AMiner Open Platform",
        "endpoint": "/api/paper/search",
        "status": "not_attempted",
        "query_title": clean_title,
        "local_authors": local_authors,
        "local_year": manuscript_year,
        "resolved_pub_id": None,
        "candidates": [],
        "api_calls": calls,
    }
    if not clean_title:
        result.update(status="not_attempted", reason="missing_local_title")
        result["cost"] = _cost_summary(calls)
        return result
    headers = aminer_headers(api_key)
    with client_factory(
        timeout=30.0,
        proxy=proxy,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        try:
            params: dict[str, Any] = {
                "query": clean_title,
                "year_to": cutoff.year,
                "page": 1,
                "size": 20,
            }
            response = client.get(AMINER_PAPER_SEARCH_URL, headers=headers, params=params)
            rows = _rows(_response_data(response, "paper_search title resolution"))
            _record_call(
                calls,
                "paper_search",
                status="completed",
                query=clean_title,
                query_shape="exact_title",
                request_method="GET",
                request_parameters=params,
                route_reason="Resolve the parsed manuscript title with the documented free title endpoint.",
                response_metadata=response.extensions.get("aminer_api_diagnostic", {}),
            )
        except Exception as exc:
            _record_call(
                calls,
                "paper_search",
                status="failed",
                query=clean_title,
                query_shape="exact_title",
                request_method="GET",
                request_parameters={
                    "query": clean_title,
                    "year_to": cutoff.year,
                    "page": 1,
                    "size": 20,
                },
                route_reason="Resolve the parsed manuscript title with the documented free title endpoint.",
                detail=f"{type(exc).__name__}: {exc}",
                response_metadata=getattr(exc, "diagnostic", {}),
            )
            result.update(status="provider_failed", reason=f"{type(exc).__name__}: {exc}")
            result["cost"] = _cost_summary(calls)
            return result

        exact_rows = [
            row
            for row in rows
            if _paper_id(row)
            and normalize_title(_title(row.get("title") or row.get("title_zh")))
            == normalize_title(clean_title)
        ]
        exact_ids = list(dict.fromkeys(_paper_id(row) for row in exact_rows))
        info_by_id: dict[str, dict[str, Any]] = {}
        if exact_ids:
            try:
                response = client.post(
                    AMINER_PAPER_INFO_URL,
                    headers=headers,
                    json={"ids": exact_ids[:100]},
                )
                info_rows = _rows(_response_data(response, "paper_info title resolution"))
                info_by_id = {
                    paper_id: row
                    for row in info_rows
                    if (paper_id := _paper_id(row))
                }
                _record_call(
                    calls,
                    "paper_info",
                    status="completed",
                    request_method="POST",
                    request_parameters={"ids": exact_ids[:100]},
                    route_reason="Batch-enrich exact-title identity candidates.",
                    response_metadata=response.extensions.get(
                        "aminer_api_diagnostic", {}
                    ),
                )
            except Exception as exc:
                _record_call(
                    calls,
                    "paper_info",
                    status="failed",
                    request_method="POST",
                    request_parameters={"ids": exact_ids[:100]},
                    route_reason="Batch-enrich exact-title identity candidates.",
                    detail=f"{type(exc).__name__}: {exc}",
                    response_metadata=getattr(exc, "diagnostic", {}),
                )

    local_author_keys = _author_keys(local_authors)
    verified: list[str] = []
    candidate_diagnostics: list[dict[str, Any]] = []
    for row in exact_rows:
        paper_id = _paper_id(row)
        enriched = dict(row)
        enriched.update(info_by_id.get(paper_id, {}))
        remote_authors = _author_names(enriched.get("authors"))
        if not remote_authors and enriched.get("first_author"):
            remote_authors = [_title(enriched.get("first_author"))]
        remote_author_keys = _author_keys(remote_authors)
        author_match = (
            bool(local_author_keys.intersection(remote_author_keys))
            if local_authors
            else None
        )
        try:
            remote_year = int(enriched.get("year"))
        except (TypeError, ValueError):
            remote_year = None
        year_match = (
            remote_year == manuscript_year if manuscript_year is not None else None
        )
        available_checks = [value for value in (author_match, year_match) if value is not None]
        identity_verified = bool(available_checks) and all(available_checks)
        candidate_diagnostics.append(
            {
                "pub_id": paper_id,
                "title": _title(enriched.get("title") or enriched.get("title_zh")),
                "authors": remote_authors,
                "year": remote_year,
                "exact_title": True,
                "author_match": author_match,
                "year_match": year_match,
                "identity_verified": identity_verified,
            }
        )
        if identity_verified and paper_id not in verified:
            verified.append(paper_id)

    result["candidates"] = candidate_diagnostics
    if len(verified) == 1:
        result.update(
            status="matched",
            reason="Unique exact-title candidate corroborated by available author/year metadata.",
            resolved_pub_id=verified[0],
        )
    elif len(verified) > 1:
        result.update(
            status="ambiguous",
            reason="Multiple candidates passed title and metadata checks; no ID was claimed.",
        )
    elif exact_rows and not local_authors and manuscript_year is None:
        result.update(
            status="insufficient_identity_evidence",
            reason=(
                "Exact-title candidates were found, but the PDF supplied no "
                "author or publication-year evidence; no ID was claimed."
            ),
        )
    elif exact_rows:
        result.update(
            status="unverified",
            reason="Exact-title candidates did not pass available author/year checks.",
        )
    else:
        result.update(status="not_found", reason="No exact-title AMiner result was found.")
    result["cost"] = _cost_summary(calls)
    return result


_QUERY_SHAPES = {
    "exact_title",
    "title_phrase",
    "keyword_phrase",
    "natural_language",
}
_SHORTEN_STOPWORDS = {
    "a", "an", "and", "are", "can", "do", "does", "find", "for", "from",
    "how", "in", "is", "of", "on", "or", "paper", "papers", "prior", "the",
    "to", "what", "which", "with", "work",
}
_QUALITY_GENERIC_TOKENS = _SHORTEN_STOPWORDS.union(
    {
        "approach", "benchmark", "dataset", "evaluation", "framework",
        "learning", "method", "model", "models", "system", "task",
    }
)


def _looks_like_sentence(query: str) -> bool:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]*", query)
    lowered = query.strip().casefold()
    return (
        query.rstrip().endswith(("?", "."))
        or lowered.startswith(("how ", "what ", "which ", "find ", "identify "))
        or (len(words) >= 9 and any(f" {verb} " in f" {lowered} " for verb in ("use", "uses", "address", "solve", "improve", "compare")))
    )


def _short_phrase(query: str, *, limit: int = 6) -> str:
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9_+.-]*", query)
    selected = [token for token in tokens if token.casefold() not in _SHORTEN_STOPWORDS]
    if not selected:
        selected = tokens
    return " ".join(selected[:limit]).strip()


def _quality_tokens(value: Any) -> set[str]:
    return {
        token.casefold()
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_+.-]*", str(value or ""))
        if len(token) >= 3 and token.casefold() not in _QUALITY_GENERIC_TOKENS
    }


def _ordered_content_tokens(value: Any) -> list[str]:
    """Return stable search terms without sentence glue or generic filler."""
    seen: set[str] = set()
    tokens: list[str] = []
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_+.-]*", str(value or "")):
        key = token.casefold()
        if len(key) < 3 or key in _QUALITY_GENERIC_TOKENS or key in seen:
            continue
        tokens.append(token)
        seen.add(key)
    return tokens


def _keyword_batch_terms(query_plans: list[dict[str, Any]]) -> list[str]:
    """Choose one coherent two-to-three-term theme for the AND-style endpoint.

    ``paper_list_by_keywords`` accepts a keyword array whose members may be
    interpreted conjunctively.  Mixing three separate long query themes makes
    an accidental impossible conjunction, so one non-title plan supplies a
    small set of atomic content terms or the rescue call is skipped.
    """
    role_priority = {
        "method_mechanism": 0,
        "problem_setting": 1,
        "direct_baseline": 2,
        "benchmark_or_evaluation": 3,
        "alternative_formulation": 4,
        "named_prior_work": 5,
    }
    ranked = sorted(
        enumerate(query_plans),
        key=lambda pair: (
            role_priority.get(str(pair[1].get("role") or ""), 9),
            pair[0],
        ),
    )
    for _index, plan in ranked:
        if str(plan.get("query_shape") or "") == "exact_title":
            continue
        tokens = _ordered_content_tokens(
            plan.get("fallback_phrase") or plan.get("query")
        )
        if len(tokens) >= 2:
            return tokens[:3]
    return []


def _row_query_quality(row: dict[str, Any], plan: dict[str, Any]) -> bool:
    """Lexical routing/ranking hint, never a discovery admission rule."""
    title_tokens = _quality_tokens(row.get("title") or row.get("title_zh"))
    query_tokens = _quality_tokens(plan.get("fallback_phrase") or plan.get("query"))
    if not title_tokens or not query_tokens:
        return False
    overlap = len(title_tokens.intersection(query_tokens))
    shape = str(plan.get("query_shape") or "")
    if shape == "exact_title":
        return normalize_title(_title(row.get("title") or row.get("title_zh"))) == normalize_title(
            str(plan.get("query") or "")
        )
    return overlap >= (1 if len(query_tokens) <= 3 else 2)


def _inspect_search_rows(rows: list[dict[str, Any]], plan: dict[str, Any],
                         tier: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Audit bibliographic rows before filtering; discovery needs no title match."""
    retained, audit = [], []
    exact = plan.get("query_shape") == "exact_title"
    for index, row in enumerate(rows):
        paper_id = _paper_id(row)
        title = _title(row.get("title") or row.get("title_zh"))
        hint = _row_query_quality(row, plan)
        disposition = ("invalid_identity" if not paper_id or not title else
                       "exact_title_mismatch" if exact and not hint else
                       "retained_for_semantic_selection")
        audit.append({
            "provider_row_index": index, "provider_id": str(row.get("id") or row.get("_id") or row.get("paper_id") or ""),
            "aminer_pub_id": paper_id, "title": title,
            "authors": _author_names(row.get("authors")), "doi": _title(row.get("doi")),
            "published": _published_value(row), "year": row.get("year"),
            "publication_date_evidence": _publication_evidence(row, "search_response"),
            "abstract_fields": {k: row[k] for k in ("abstract", "abstract_zh", "abstract_slice", "abstract_zh_slice")
                                if isinstance(row.get(k), str)},
            "title_overlap_hint": hint, "disposition": disposition,
        })
        if disposition == "retained_for_semantic_selection":
            retained.append({**row, "_rw_route_tier": tier, "_rw_title_overlap_hint": hint})
    return retained, {
        "query": plan["query"], "query_shape": plan["query_shape"], "tier": tier,
        "policy": "exact_identity_only" if exact else "title_overlap_ranking_only",
        "provider_record_count": len(rows), "retained_record_count": len(retained),
        "records": audit,
    }


def _prequalified_pool(
    records: list[dict[str, Any]],
    *,
    cutoff: date,
) -> tuple[set[str], set[str]]:
    qualified_ids: set[str] = set()
    qualified_roles: set[str] = set()
    for record in records:
        paper_id = str(record.get("aminer_pub_id") or "")
        published = str(record.get("published") or "").strip()
        if not paper_id or not published:
            continue
        year_match = re.match(r"^(\d{4})", published)
        if not year_match or int(year_match.group(1)) > cutoff.year:
            continue
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", published):
            try:
                if date.fromisoformat(published) > cutoff:
                    continue
            except ValueError:
                continue
        plan = {
            "query": record.get("query"),
            "fallback_phrase": record.get("fallback_phrase") or record.get("query"),
            "query_shape": record.get("query_shape"),
        }
        row = {"title": record.get("title")}
        if not _row_query_quality(row, plan):
            continue
        qualified_ids.add(paper_id)
        role = str(record.get("query_role") or "").strip()
        if role:
            qualified_roles.add(role)
    return qualified_ids, qualified_roles


def _normalize_query_plan(value: Any, index: int) -> dict[str, Any]:
    if isinstance(value, dict):
        query = _title(value.get("query") or value.get("text"))
        shape = str(value.get("query_shape") or value.get("shape") or "").strip().lower()
        query_subtype = str(value.get("query_subtype") or "").strip().lower()
        mechanism_branch = _title(value.get("mechanism_branch"))
        feedback_origin = _title(value.get("feedback_origin"))
        fallback_phrase = _title(value.get("fallback_phrase"))
        role = _title(value.get("role"))
        pro_field = str(value.get("pro_field") or "").strip().lower()
        facet = str(value.get("facet") or "").strip().lower()
    else:
        query = _title(value)
        shape = str(getattr(value, "query_shape", "") or "").strip().lower()
        query_subtype = str(
            getattr(value, "query_subtype", "") or ""
        ).strip().lower()
        mechanism_branch = _title(getattr(value, "mechanism_branch", ""))
        feedback_origin = _title(getattr(value, "feedback_origin", ""))
        fallback_phrase = _title(getattr(value, "fallback_phrase", ""))
        role = _title(getattr(value, "role", ""))
        pro_field = str(getattr(value, "pro_field", "") or "").strip().lower()
        facet = str(getattr(value, "facet", "") or "").strip().lower()
    if shape not in _QUERY_SHAPES:
        word_count = len(re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]*", query))
        if _looks_like_sentence(query):
            shape = "natural_language"
        elif word_count <= 4:
            shape = "title_phrase"
        else:
            shape = "keyword_phrase"
    elif shape == "exact_title":
        word_count = len(
            re.findall(r"[A-Za-z0-9][A-Za-z0-9_+.-]*", query)
        )
        if word_count < 4:
            shape = "title_phrase"
    if pro_field not in {"title", "keyword", "abstract"}:
        pro_field = "keyword" if shape == "keyword_phrase" else "title"
    if query_subtype != CONTROLLED_MECHANISM_DOMAIN:
        query_subtype = ""
    elif shape != "keyword_phrase":
        query_subtype = ""
    else:
        # The allowlisted subtype, rather than a legacy pro_field hint, is the
        # authority for the narrow fielded-title route.
        pro_field = "title"
    if query_subtype != CONTROLLED_MECHANISM_DOMAIN:
        mechanism_branch = ""
    if not fallback_phrase or _looks_like_sentence(fallback_phrase):
        fallback_phrase = _short_phrase(query)
    return {
        "query": query,
        "coverage_target_id": str(value.get("target_id") or "") if isinstance(value, dict) else str(getattr(value, "target_id", "") or ""),
        "query_shape": shape,
        "query_subtype": query_subtype,
        "mechanism_branch": mechanism_branch,
        "feedback_origin": feedback_origin,
        "fallback_phrase": fallback_phrase,
        "role": role or f"query_{index + 1}",
        "pro_field": pro_field,
        "facet": facet,
    }


def _route_chain(plan: dict[str, Any], search_tier: str) -> list[str]:
    if search_tier == "free":
        return ["free"]
    if search_tier == "pro":
        return ["pro", "free"]
    if search_tier == "qa":
        return ["qa", "pro", "free"]
    if search_tier == "qa-pro":
        return ["qa-pro", "qa", "pro", "free"]
    shape = str(plan["query_shape"])
    if shape == "natural_language":
        # The current organization token can use legacy QA but not QA Pro.
        # Explicit --aminer-search-tier qa-pro remains the capability probe.
        return ["qa", "pro", "free"]
    if shape == "keyword_phrase":
        return ["pro", "free"]
    return ["free", "pro"]


def _api_name(tier: str) -> str:
    return {
        "free": "paper_search",
        "pro": "paper_search_pro",
        "qa": "paper_qa_search",
        "qa-pro": "paper_qa_search_pro",
    }[tier]


def _search_once_request(
    plan: dict[str, Any], tier: str, cutoff_year: int
) -> tuple[str, str, str, dict[str, Any]]:
    original_query = str(plan["query"])
    fallback_phrase = str(plan["fallback_phrase"])
    if tier == "qa-pro":
        body = {
            "query": original_query,
            "query_type": "auto",
            "year_to": cutoff_year,
            "sort": "relevance",
        }
        return AMINER_PAPER_QA_SEARCH_PRO_URL, "paper_qa_search_pro", "POST", body
    if tier == "qa":
        body = {
            "use_topic": True,
            "query": original_query,
            "size": 20,
            "offset": 0,
        }
        return AMINER_PAPER_QA_SEARCH_URL, "paper_qa_search", "POST", body
    if tier == "pro":
        search_text = (
            original_query
            if str(plan["query_shape"]) == "exact_title"
            else fallback_phrase
        )
        params: dict[str, Any] = {
            "page": 0,
            "size": 100,
            "year_to": cutoff_year,
        }
        fielded_title = (
            str(plan["query_shape"]) == "exact_title"
            or str(plan.get("query_subtype") or "")
            == CONTROLLED_MECHANISM_DOMAIN
        )
        if fielded_title:
            params["title"] = search_text
        else:
            # v1.5.0 defines `query` as the relevance-ranked title+keywords
            # search box.  Fielded `title` is reserved for a verified exact
            # title or the allowlisted mechanism+domain feedback subtype.
            params["query"] = search_text
        return AMINER_PAPER_SEARCH_PRO_URL, "paper_search_pro", "GET", params
    search_text = original_query
    if str(plan["query_shape"]) in {"natural_language", "keyword_phrase"}:
        search_text = fallback_phrase
    params = {
        "query": search_text,
        "year_to": cutoff_year,
        "page": 1,
        "size": 20,
    }
    return AMINER_PAPER_SEARCH_URL, "paper_search", "GET", params


def _search_once(
    client: Any,
    *,
    tier: str,
    plan: dict[str, Any],
    cutoff_year: int,
    headers: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, Any], str, dict[str, Any]]:
    url, api, method, parameters = _search_once_request(plan, tier, cutoff_year)
    if method == "POST":
        response = client.post(url, headers=headers, json=parameters)
    else:
        response = client.get(url, headers=headers, params=parameters)
    rows = _rows(_response_data(response, api))
    return (
        rows,
        dict(response.extensions.get("aminer_api_diagnostic", {})),
        method,
        parameters,
    )


def retrieve_aminer_records(
    queries: list[Any],
    *,
    review_as_of: str,
    api_key: str,
    proxy: str | None,
    search_tier: str = "auto",
    detail_limit: int = 0,
    allow_keyword_expansion: bool = True,
    client_factory: Callable[..., Any] = httpx.Client,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Retrieve candidates from AMiner without silently crossing providers."""
    if search_tier not in {"auto", "free", "pro", "qa", "qa-pro"}:
        raise ValueError("AMiner search tier must be auto, free, pro, qa, or qa-pro.")
    cutoff = date.fromisoformat(review_as_of)
    headers = aminer_headers(api_key)
    calls: list[dict[str, Any]] = []
    downgrades: list[dict[str, str]] = []
    errors: list[str] = []
    warnings: list[str] = []
    records: list[dict[str, Any]] = []
    search_responses: list[dict[str, Any]] = []
    query_plans = [
        _normalize_query_plan(query, index)
        for index, query in enumerate(queries)
        if _title(query.get("query") if isinstance(query, dict) else query)
    ]

    with client_factory(
        timeout=30.0,
        proxy=proxy,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        for plan in query_plans:
            original_query = str(plan["query"])
            tier_order = _route_chain(plan, search_tier)
            query_rows: list[dict[str, Any]] = []
            completed_once = False
            last_error = ""
            selected_tier = tier_order[-1]
            for index, tier in enumerate(tier_order):
                api = _api_name(tier)
                route_reason = {
                    "free": "Exact title or short title phrase; use the documented free title endpoint.",
                    "pro": "Controlled phrase or bulk candidate expansion; use relevance-ranked query search.",
                    "qa": "Natural-language sentence; use the available legacy QA semantic endpoint.",
                    "qa-pro": "Explicit QA-Pro capability selection for natural-language precision.",
                }[tier]
                if (
                    tier == "pro"
                    and str(plan.get("query_subtype") or "")
                    == CONTROLLED_MECHANISM_DOMAIN
                ):
                    route_reason = (
                        "Controlled feedback mechanism+domain phrase; use the "
                        "documented fielded title search."
                    )
                elif (
                    tier == "free"
                    and str(plan.get("query_subtype") or "")
                    == CONTROLLED_MECHANISM_DOMAIN
                ):
                    route_reason = (
                        "Fallback after controlled mechanism+domain Pro search; "
                        "use the documented free query endpoint."
                    )
                try:
                    route_rows, response_metadata, method, request_parameters = _search_once(
                        client,
                        tier=tier,
                        plan=plan,
                        cutoff_year=cutoff.year,
                        headers=headers,
                    )
                    completed_once = True
                    retained_rows, response_audit = _inspect_search_rows(route_rows, plan, tier)
                    search_responses.append(response_audit)
                    query_rows.extend(retained_rows)
                    qualified_rows = [
                        row for row in retained_rows if _row_query_quality(row, plan)
                    ]
                    result_quality_sufficient = bool(qualified_rows)
                    if str(plan.get("query_shape") or "") == "natural_language":
                        result_quality_sufficient = bool(qualified_rows) or len(retained_rows) >= 3
                    _record_call(
                        calls,
                        api,
                        status=(
                            "completed"
                            if result_quality_sufficient
                            else "completed_low_quality"
                            if route_rows
                            else "completed_empty"
                        ),
                        query=original_query,
                        query_shape=str(plan["query_shape"]),
                        query_subtype=str(plan.get("query_subtype") or ""),
                        mechanism_branch=str(plan.get("mechanism_branch") or ""),
                        feedback_origin=str(plan.get("feedback_origin") or ""),
                        request_method=method,
                        request_parameters=request_parameters,
                        route_reason=route_reason,
                        response_metadata=response_metadata,
                    )
                    if result_quality_sufficient:
                        selected_tier = tier
                        break
                    if index + 1 < len(tier_order):
                        downgrades.append(
                            {
                                "query": original_query,
                                "query_shape": str(plan["query_shape"]),
                                "query_subtype": str(plan.get("query_subtype") or ""),
                                "mechanism_branch": str(plan.get("mechanism_branch") or ""),
                                "from": tier,
                                "to": tier_order[index + 1],
                                "trigger": (
                                    "low_quality_result" if route_rows else "empty_result"
                                ),
                                "reason": (
                                    "No valid row supplied the title routing hint. Keep discovery "
                                    "rows and try the next documented route within its existing budget."
                                    if route_rows
                                    else "The AMiner endpoint completed but returned no candidate papers."
                                ),
                            }
                        )
                except Exception as exc:
                    detail = f"{type(exc).__name__}: {exc}"
                    last_error = detail
                    # Reconstruct the documented request shape for diagnostics
                    # even when the HTTP client raises before returning.
                    try:
                        _, _, method, request_parameters = _search_once_request(plan, tier, cutoff.year)
                    except Exception:
                        method, request_parameters = "UNKNOWN", {}
                    _record_call(
                        calls,
                        api,
                        status="failed",
                        query=original_query,
                        query_shape=str(plan["query_shape"]),
                        query_subtype=str(plan.get("query_subtype") or ""),
                        mechanism_branch=str(plan.get("mechanism_branch") or ""),
                        feedback_origin=str(plan.get("feedback_origin") or ""),
                        request_method=method,
                        request_parameters=request_parameters,
                        route_reason=route_reason,
                        detail=detail,
                        response_metadata=getattr(exc, "diagnostic", {}),
                    )
                    if index + 1 < len(tier_order):
                        downgrades.append(
                            {
                                "query": original_query,
                                "query_shape": str(plan["query_shape"]),
                                "query_subtype": str(plan.get("query_subtype") or ""),
                                "mechanism_branch": str(plan.get("mechanism_branch") or ""),
                                "from": tier,
                                "to": tier_order[index + 1],
                                "trigger": "api_error",
                                "reason": detail[:240],
                            }
                        )
            if not query_rows:
                if not completed_once and last_error:
                    errors.append(
                        f"AMiner search failed for query {original_query!r}: {last_error}"
                    )
                else:
                    warnings.append(
                        f"AMiner search returned no candidates for query {original_query!r}."
                    )
            # Stable soft ranking; false hints remain eligible and auditable.
            query_rows.sort(key=lambda row: not row.get("_rw_title_overlap_hint", False))
            for row in query_rows:
                paper_id = _paper_id(row)
                title = _title(row.get("title") or row.get("title_zh"))
                if not paper_id or not title:
                    continue
                authors = _author_names(row.get("authors"))
                if not authors and row.get("first_author"):
                    authors = [_title(row.get("first_author"))]
                records.append(
                    {
                        "aminer_pub_id": paper_id,
                        "title": title,
                        "authors": ", ".join(authors),
                        "published": _published_value(row),
                        "publication_date_evidence": _publication_evidence(row, "search"),
                        "year": row.get("year"),
                        "venue": _title(row.get("venue_name") or row.get("raw")),
                        "doi": _title(row.get("doi")),
                        "url": f"https://www.aminer.cn/pub/{paper_id}",
                        "summary": _abstract_value(row),
                        "query": original_query,
                        "fallback_phrase": str(plan.get("fallback_phrase") or ""),
                        "query_shape": str(plan["query_shape"]),
                        "query_subtype": str(plan.get("query_subtype") or ""),
                        "mechanism_branch": str(plan.get("mechanism_branch") or ""),
                        "feedback_origin": str(plan.get("feedback_origin") or ""),
                        "query_role": str(plan["role"]),
                        "query_facet": str(plan.get("facet") or ""),
                        "query_title_overlap_hint": row.get("_rw_title_overlap_hint", False),
                        "source": f"AMiner Open Platform {_api_name(row.get('_rw_route_tier', selected_tier))}",
                        "retrieval_provider": "aminer_open_platform",
                        "retrieval_tier": row.get("_rw_route_tier", selected_tier),
                    }
                )

        unique_ids = list(
            dict.fromkeys(
                str(record.get("aminer_pub_id") or "")
                for record in records
                if record.get("aminer_pub_id")
            )
        )
        prequalified_ids, prequalified_roles = _prequalified_pool(
            records, cutoff=cutoff
        )
        planned_roles = {
            str(plan.get("role") or "").strip()
            for plan in query_plans
            if str(plan.get("role") or "").strip()
        }
        required_role_coverage = min(4, len(planned_roles))
        quality_pool_small = len(prequalified_ids) < 8
        role_coverage_small = (
            required_role_coverage > 0
            and len(prequalified_roles.intersection(planned_roles))
            < required_role_coverage
        )
        keyword_expansion_diagnostic: dict[str, Any] = {
            "attempted": False,
            "trigger": "not_needed",
            "raw_unique_candidate_count": len(unique_ids),
            "prequalified_candidate_count": len(prequalified_ids),
            "qualified_role_count": len(
                prequalified_roles.intersection(planned_roles)
            ),
            "required_role_coverage": required_role_coverage,
            "maximum_calls": 1 if allow_keyword_expansion else 0,
        }
        if not allow_keyword_expansion:
            keyword_expansion_diagnostic.update(
                trigger="disabled_by_cross_round_call_budget",
            )
        # This thematic endpoint is broader and can drift, so it is a single,
        # bounded rescue call only after the shape-aware routes produced too
        # small a pool.  The later cutoff, dedupe, and model reranking remain
        # authoritative.
        if (
            search_tier == "auto"
            and len(query_plans) >= 2
            and allow_keyword_expansion
            and (quality_pool_small or role_coverage_small)
        ):
            topic_terms = _keyword_batch_terms(query_plans)
            if len(topic_terms) >= 2:
                trigger_reasons = []
                if quality_pool_small:
                    trigger_reasons.append("prequalified_candidates_below_8")
                if role_coverage_small:
                    trigger_reasons.append("qualified_role_coverage_below_target")
                keyword_expansion_diagnostic.update(
                    attempted=True,
                    trigger="+".join(trigger_reasons),
                    topic_terms=topic_terms,
                    term_strategy="single_coherent_plan_atomic_terms",
                )
                params = {
                    "page": 0,
                    "size": 20,
                    "keywords": json.dumps(topic_terms, ensure_ascii=False),
                }
                try:
                    response = client.get(
                        AMINER_PAPER_LIST_BY_KEYWORDS_URL,
                        headers=headers,
                        params=params,
                    )
                    batch_rows = _rows(
                        _response_data(response, "paper_list_by_keywords")
                    )
                    _, batch_audit = _inspect_search_rows(batch_rows, {
                        "query": " | ".join(topic_terms), "fallback_phrase": " ".join(topic_terms),
                        "query_shape": "multi_keyword_fallback",
                    }, "keyword-batch")
                    search_responses.append(batch_audit)
                    _record_call(
                        calls,
                        "paper_list_by_keywords",
                        status="completed" if batch_rows else "completed_empty",
                        query=" | ".join(topic_terms),
                        query_shape="multi_keyword_fallback",
                        request_method="GET",
                        request_parameters=params,
                        route_reason=(
                            "Prequalified candidates or qualified role coverage "
                            "are below target; run one bounded thematic AMiner "
                            "expansion with two or three atomic terms from one "
                            "coherent query before cutoff filtering and reranking."
                        ),
                        response_metadata=response.extensions.get(
                            "aminer_api_diagnostic", {}
                        ),
                    )
                    for row in batch_rows:
                        paper_id = _paper_id(row)
                        title = _title(row.get("title") or row.get("title_zh"))
                        if not paper_id or not title:
                            continue
                        authors = _author_names(row.get("authors"))
                        records.append(
                            {
                                "aminer_pub_id": paper_id,
                                "title": title,
                                "authors": ", ".join(authors),
                                "published": _published_value(row),
                                "publication_date_evidence": _publication_evidence(row, "keyword_search"),
                                "year": row.get("year"),
                                "venue": _title(row.get("venue_name") or row.get("raw")),
                                "doi": _title(row.get("doi")),
                                "url": f"https://www.aminer.cn/pub/{paper_id}",
                                "summary": _abstract_value(row),
                                "query": " | ".join(topic_terms),
                                "query_shape": "multi_keyword_fallback",
                                "query_role": "candidate_pool_expansion",
                                "source": "AMiner Open Platform paper_list_by_keywords",
                                "retrieval_provider": "aminer_open_platform",
                                "retrieval_tier": "keyword-batch",
                            }
                        )
                except Exception as exc:
                    detail = f"{type(exc).__name__}: {exc}"
                    _record_call(
                        calls,
                        "paper_list_by_keywords",
                        status="failed",
                        query=" | ".join(topic_terms),
                        query_shape="multi_keyword_fallback",
                        request_method="GET",
                        request_parameters=params,
                        route_reason=(
                            "Prequalified candidates or role coverage are below "
                            "target; optional bounded thematic expansion."
                        ),
                        detail=detail,
                        response_metadata=getattr(exc, "diagnostic", {}),
                    )
                    warnings.append(
                        f"AMiner paper_list_by_keywords expansion failed: {detail}"
                    )
                unique_ids = list(
                    dict.fromkeys(
                        str(record.get("aminer_pub_id") or "")
                        for record in records
                        if record.get("aminer_pub_id")
                    )
                )
            else:
                keyword_expansion_diagnostic.update(
                    trigger="unsafe_no_coherent_short_theme",
                    topic_terms=[],
                    term_strategy="skipped_instead_of_joining_long_query_themes",
                )
        info_by_id: dict[str, dict[str, Any]] = {}
        for start in range(0, len(unique_ids), 100):
            batch = unique_ids[start : start + 100]
            try:
                response = client.post(
                    AMINER_PAPER_INFO_URL,
                    headers=headers,
                    json={"ids": batch},
                )
                for row in _rows(_response_data(response, "paper_info")):
                    if paper_id := _paper_id(row):
                        info_by_id[paper_id] = row
                _record_call(
                    calls,
                    "paper_info",
                    status="completed",
                    request_method="POST",
                    request_parameters={"ids": batch},
                    route_reason="Batch-enrich retrieved candidates with free abstract slices and metadata.",
                    response_metadata=response.extensions.get(
                        "aminer_api_diagnostic", {}
                    ),
                )
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                _record_call(
                    calls,
                    "paper_info",
                    status="failed",
                    request_method="POST",
                    request_parameters={"ids": batch},
                    route_reason="Batch-enrich retrieved candidates with free abstract slices and metadata.",
                    detail=detail,
                    response_metadata=getattr(exc, "diagnostic", {}),
                )
                warnings.append(f"AMiner paper_info enrichment failed: {detail}")

        detail_by_id: dict[str, dict[str, Any]] = {}
        if detail_limit > 0:
            detail_ids = [
                paper_id
                for paper_id in unique_ids
                if not str(info_by_id.get(paper_id, {}).get("abstract_slice") or "").strip()
            ][:detail_limit]
            for paper_id in detail_ids:
                try:
                    response = client.get(
                        AMINER_PAPER_DETAIL_URL,
                        headers=headers,
                        params={"id": paper_id},
                    )
                    detail_rows = _rows(_response_data(response, "paper_detail"))
                    if detail_rows:
                        detail_by_id[paper_id] = detail_rows[0]
                    _record_call(
                        calls,
                        "paper_detail",
                        status="completed",
                        request_method="GET",
                        request_parameters={"id": paper_id},
                        route_reason="Bounded full-detail enrichment because paper_info had no abstract slice.",
                        response_metadata=response.extensions.get(
                            "aminer_api_diagnostic", {}
                        ),
                    )
                except Exception as exc:
                    detail = f"{type(exc).__name__}: {exc}"
                    _record_call(
                        calls,
                        "paper_detail",
                        status="failed",
                        request_method="GET",
                        request_parameters={"id": paper_id},
                        route_reason="Bounded full-detail enrichment because paper_info had no abstract slice.",
                        detail=detail,
                        response_metadata=getattr(exc, "diagnostic", {}),
                    )
                    warnings.append(f"AMiner paper_detail {paper_id} failed: {detail}")

    for record in records:
        paper_id = str(record.get("aminer_pub_id") or "")
        info = info_by_id.get(paper_id, {})
        detail = detail_by_id.get(paper_id, {})
        authors = _author_names(info.get("authors") or detail.get("authors"))
        if authors:
            record["authors"] = ", ".join(authors)
        record["summary"] = max(
            (str(record.get("summary") or ""), _abstract_value(info), _abstract_value(detail)), key=len,
        )[:10000]
        evidence = list(record.get("publication_date_evidence") or [])
        evidence.extend(_publication_evidence(info, "paper_info"))
        evidence.extend(_publication_evidence(detail, "paper_detail"))
        record["publication_date_evidence"] = evidence
        record["publication_date_basis"] = "earliest_supported_availability_window"
        record["publication_year_conflict"] = len({item["value"][:4] for item in evidence}) > 1
        enriched_published = _choose_publication_evidence(evidence)
        if enriched_published:
            record["published"] = enriched_published
        if not record.get("year") and (info.get("year") or detail.get("year")):
            record["year"] = info.get("year") or detail.get("year")
            if not record.get("published"):
                record["published"] = str(record["year"])
        venue = info.get("venue") or detail.get("venue")
        if isinstance(venue, dict):
            venue = venue.get("name") or venue.get("raw") or venue.get("name_zh")
        if venue:
            record["venue"] = _title(venue)
        for field in ("doi", "arxiv_id"):
            if not record.get(field) and (info.get(field) or detail.get(field)):
                record[field] = _title(info.get(field) or detail.get(field))

    diagnostics = {
        "provider": "aminer_open_platform",
        "provider_contract": "AMinerOrg aminer-academic-search v1.5.0 official API catalog",
        "requested_search_tier": search_tier,
        "query_plan": query_plans,
        "search_responses": search_responses,
        "search_response_policy": "bibliographic_rows_before_identity_or_title_filtering; no_headers",
        "external_fallback_enabled": False,
        "external_fallback_used": False,
        "downgrades": downgrades,
        "provider_errors": errors,
        "provider_warnings": warnings,
        "api_calls": calls,
        "cost": _cost_summary(calls),
        "paper_info_record_count": len(info_by_id),
        "paper_detail_record_count": len(detail_by_id),
        "raw_record_count": len(records),
        "keyword_expansion": keyword_expansion_diagnostic,
    }
    return records, diagnostics


__all__ = [
    "AMINER_API_PRICES_YUAN",
    "AMINER_PAPER_DETAIL_URL",
    "AMINER_PAPER_INFO_URL",
    "AMINER_PAPER_LIST_BY_KEYWORDS_URL",
    "AMINER_PAPER_QA_SEARCH_URL",
    "AMINER_PAPER_QA_SEARCH_PRO_URL",
    "AMINER_PAPER_SEARCH_PRO_URL",
    "AMINER_PAPER_SEARCH_URL",
    "aminer_headers",
    "normalize_title",
    "resolve_title_to_pub_id",
    "retrieve_aminer_records",
]
