"""Self-contained AMiner backward-reference expansion for Related Work Audit.

The standalone Skill deliberately owns this transport.  It does not import the
AMiner research-skills client, repository Core, or the Demo server.
"""

from __future__ import annotations


from datetime import date
from typing import Any, Callable

import httpx

from .aminer import normalize_pub_id
from .identity import aminer_headers
from .publication_time import date_bounds


AMINER_PAPER_RELATION_URL = (
    "https://datacenter.aminer.cn/gateway/open_platform/api/paper/relation"
)
AMINER_PAPER_INFO_URL = (
    "https://datacenter.aminer.cn/gateway/open_platform/api/paper/info"
)


class AMinerRelationError(ValueError):
    def __init__(self, message: str, diagnostic: dict[str, Any]):
        super().__init__(message)
        self.diagnostic = diagnostic


def _response_data(response: httpx.Response, label: str) -> Any:
    try:
        payload = response.json()
    except ValueError as exc:
        diagnostic = {"http_status": response.status_code}
        response.extensions["aminer_api_diagnostic"] = diagnostic
        raise AMinerRelationError(
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
        raise AMinerRelationError(
            f"AMiner {label} returned HTTP {response.status_code}.", diagnostic
        )
    if isinstance(payload, dict):
        code = payload.get("code")
        success = payload.get("success")
        if success is False or (code is not None and code not in {0, 200, "0", "200"}):
            raise AMinerRelationError(
                f"AMiner {label} returned API code {code!s}.", diagnostic
            )
        return payload.get("data")
    return payload


def _rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return []
    for key in ("items", "records", "relations", "list", "data"):
        nested = value.get(key)
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
    return [value]


def _paper_ids(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    identifiers: list[str] = []
    for item in values:
        if isinstance(item, dict):
            item = item.get("id") or item.get("_id") or item.get("pub_id")
        raw = str(item or "").strip().lower()
        try:
            normalized = normalize_pub_id(raw)
        except ValueError:
            continue
        if normalized not in identifiers:
            identifiers.append(normalized)
    return identifiers


def _related_ids(data: Any, *, field: str, target_pub_id: str) -> list[str]:
    identifiers: list[str] = []
    for row in _rows(data):
        for identifier in _paper_ids(row.get(field)):
            if identifier != target_pub_id and identifier not in identifiers:
                identifiers.append(identifier)
    return identifiers


def _author_names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if isinstance(item, dict):
            name = item.get("name") or item.get("name_zh")
        else:
            name = item
        rendered = " ".join(str(name or "").split())
        if rendered:
            names.append(rendered)
    return names


def _time_fields(row: dict[str, Any], review_as_of: str) -> dict[str, Any]:
    cutoff = date.fromisoformat(review_as_of)
    # Validate all supported precisions before deriving admissibility. A year
    # alone is sufficient at the product's normalized year-end boundary.
    raw = next((str(row.get(key) or "").strip() for key in
                ("published", "publication_date", "date", "year")
                if date_bounds(row.get(key))), "")
    bounds = date_bounds(raw)
    if not bounds:
        return {"published": "", "date_precision": "unknown",
                "cutoff_status": "undated", "time_admissibility": "unknown"}
    precision = "year" if len(raw) == 4 else "month" if len(raw) == 7 else "day"
    published = raw if precision != "day" else bounds[0].isoformat()
    if bounds[1] <= cutoff:
        status = {"day": "admissible_exact_date", "month": "admissible_month_before_cutoff",
                  "year": "admissible_year_through_cutoff" if bounds[0].year == cutoff.year
                          else "admissible_year_before_cutoff"}[precision]
        admissibility = "admissible"
    elif bounds[0] > cutoff:
        status = {"day": "after_cutoff_exact_date", "month": "after_cutoff_month",
                  "year": "after_cutoff_year"}[precision]
        admissibility = "inadmissible"
    else:
        status = "uncertain_same_year" if precision == "year" else "uncertain_cutoff_month"
        admissibility = "uncertain"
    return {"published": published, "date_precision": precision,
            "cutoff_status": status, "time_admissibility": admissibility}


def _metadata_record(
    row: dict[str, Any],
    *,
    relation_role: str,
    target_pub_id: str,
    review_as_of: str,
) -> dict[str, Any] | None:
    identifiers = _paper_ids(row.get("id") or row.get("_id") or row.get("pub_id"))
    if not identifiers:
        return None
    paper_id = identifiers[0]
    title = " ".join(str(row.get("title") or row.get("title_zh") or "").split())
    time_fields = _time_fields(row, review_as_of)
    try:
        year: int | None = int(row.get("year"))
    except (TypeError, ValueError):
        year = None
    venue_value = row.get("venue")
    if isinstance(venue_value, dict):
        venue = str(
            venue_value.get("raw")
            or venue_value.get("raw_zh")
            or venue_value.get("name")
            or ""
        ).strip()
    else:
        venue = str(venue_value or row.get("raw") or "").strip()
    return {
        "aminer_pub_id": paper_id,
        "title": title,
        "authors": _author_names(row.get("authors")),
        "published": time_fields["published"],
        "year": year,
        "venue": venue,
        "venue_id": str(row.get("venue_id") or ""),
        "date_precision": time_fields["date_precision"],
        "cutoff_status": time_fields["cutoff_status"],
        "time_admissibility": time_fields["time_admissibility"],
        "summary": str(row.get("abstract_slice") or row.get("abstract_zh_slice") or "").strip(),
        "source": "AMiner Open Platform paper/relation + paper_info",
        "url": f"https://www.aminer.cn/pub/{paper_id}",
        "query": f"AMiner backward references for {target_pub_id}",
        "retrieval_role": (
            "citation_graph_reference"
        ),
        "relation_role": relation_role,
        "relation_evidence": {
            "provider": "AMiner Open Platform paper/relation",
            "target_pub_id": target_pub_id,
            "related_pub_id": paper_id,
            "direction": (
                "target_cites_related_paper"
            ),
        },
    }


def unavailable_relation_context(
    target_pub_id: str,
    review_as_of: str,
    error: str,
) -> dict[str, Any]:
    """Return a reportable, non-fatal relation failure."""
    return {
        "schema_version": "0.1",
        "provider": "AMiner Open Platform paper/relation + paper_info",
        "target_pub_id": normalize_pub_id(target_pub_id),
        "review_as_of": review_as_of,
        "status": "failed",
        "non_fatal": True,
        "queries": [],
        "metadata_status": "not_attempted",
        "summary": {
            "total_http_request_count": 0,
            "relation_api_calls_attempted": 0,
            "relation_api_calls_completed": 0,
            "paper_info_batches_attempted": 0,
            "paper_info_batches_completed": 0,
            "target_reference_ids": 0,
            "citing_target_ids": 0,
            "metadata_records": 0,
            "candidate_records": 0,
            "same_year_cutoff_uncertain": 0,
        },
        "target_references": [],
        "citing_target_records": [],
        "candidate_records": [],
        "candidate_exclusions": [],
        "metadata_missing_ids": [],
        "errors": [error],
        "provider_errors": [error],
        "limitations": [
            "AMiner backward-reference expansion failed; search-based discovery continued."
        ],
        "supported_directions": ["target_references"],
        "unsupported_directions": ["papers_citing_target"],
        "cost": {
            "currency": "CNY",
            "estimated_total_yuan": 0.0,
            "calls": 0,
        },
        "api_calls": [],
    }


def fetch_aminer_relation_context(
    target_pub_id: str,
    *,
    review_as_of: str,
    api_key: str,
    proxy: str | None,
    client_factory: Callable[..., Any] = httpx.Client,
) -> dict[str, Any]:
    """Fetch the target's backward references and resolve their metadata.

    Network/API failures are collected in the returned diagnostics and never
    raised as task-fatal errors.  The public Open Platform endpoint does not
    provide papers that cite the target, so this function never claims a
    forward-citation capability.
    """
    target = normalize_pub_id(target_pub_id)
    errors: list[str] = []
    query_diagnostics: list[dict[str, Any]] = []
    outgoing_ids: list[str] = []
    successful_queries = 0
    relation_calls_attempted = 0
    paper_info_batches_attempted = 0
    paper_info_batches_completed = 0
    api_calls: list[dict[str, Any]] = []
    headers = aminer_headers(str(api_key or "").strip())
    if not headers["Authorization"]:
        return unavailable_relation_context(
            target, review_as_of, "AMINER_API_KEY is not configured for relation expansion."
        )

    try:
        client_context = client_factory(
            timeout=30.0,
            proxy=proxy,
            follow_redirects=False,
            trust_env=False,
        )
    except Exception as exc:
        return unavailable_relation_context(
            target,
            review_as_of,
            f"AMiner relation client initialization failed: {type(exc).__name__}.",
        )

    with client_context as client:
        diagnostic = {
            "direction": "target_references",
            "parameter": "id",
            "query_value": target,
            "status": "failed",
            "related_ids": 0,
            "endpoint": "/api/paper/relation",
            "unit_price_yuan": 0.10,
        }
        try:
            relation_calls_attempted += 1
            response = client.get(
                AMINER_PAPER_RELATION_URL,
                headers=headers,
                params={"id": target},
            )
            data = _response_data(response, "paper/relation")
            response_metadata = dict(
                response.extensions.get("aminer_api_diagnostic", {})
            )
            relation_rows = _rows(data)
            outgoing_ids = _related_ids(
                data, field="cited", target_pub_id=target
            )
            diagnostic.update(
                status="completed",
                relation_rows_returned=len(relation_rows),
                related_ids=len(outgoing_ids),
                response=response_metadata,
            )
            api_calls.append(
                {
                    "api": "paper_relation",
                    "endpoint": "/api/paper/relation",
                    "status": "completed",
                    "unit_price_yuan": 0.10,
                    "response": response_metadata,
                }
            )
            successful_queries = 1
        except Exception as exc:
            message = (
                "AMiner Open Platform paper/relation failed: "
                f"{type(exc).__name__}: {exc}"
            )
            diagnostic["error"] = message
            if getattr(exc, "diagnostic", None):
                diagnostic["response"] = dict(exc.diagnostic)
            api_calls.append(
                {
                    "api": "paper_relation",
                    "endpoint": "/api/paper/relation",
                    "status": "failed",
                    "unit_price_yuan": 0.10,
                    "response": dict(getattr(exc, "diagnostic", {})),
                }
            )
            errors.append(message)
        query_diagnostics.append(diagnostic)

        metadata_ids = list(dict.fromkeys(outgoing_ids))
        metadata_rows: list[dict[str, Any]] = []
        metadata_status = "not_needed" if not metadata_ids else "failed"
        if metadata_ids:
            for start in range(0, len(metadata_ids), 100):
                batch = metadata_ids[start : start + 100]
                try:
                    paper_info_batches_attempted += 1
                    response = client.post(
                        AMINER_PAPER_INFO_URL,
                        headers=headers,
                        json={"ids": batch},
                    )
                    metadata_rows.extend(
                        _rows(_response_data(response, "paper_info"))
                    )
                    paper_info_batches_completed += 1
                    api_calls.append(
                        {
                            "api": "paper_info",
                            "endpoint": "/api/paper/info",
                            "status": "completed",
                            "unit_price_yuan": 0.0,
                            "batch_size": len(batch),
                            "response": dict(
                                response.extensions.get(
                                    "aminer_api_diagnostic", {}
                                )
                            ),
                        }
                    )
                except Exception as exc:
                    message = (
                        f"AMiner paper_info batch failed: {type(exc).__name__}: {exc}"
                    )
                    errors.append(message)
                    api_calls.append(
                        {
                            "api": "paper_info",
                            "endpoint": "/api/paper/info",
                            "status": "failed",
                            "unit_price_yuan": 0.0,
                            "batch_size": len(batch),
                            "response": dict(getattr(exc, "diagnostic", {})),
                        }
                    )
            metadata_status = (
                "completed"
                if paper_info_batches_completed == paper_info_batches_attempted
                else "partial"
                if paper_info_batches_completed
                else "failed"
            )

    metadata_by_id: dict[str, dict[str, Any]] = {}
    for row in metadata_rows:
        identifiers = _paper_ids(row.get("id") or row.get("_id") or row.get("pub_id"))
        if identifiers:
            metadata_by_id[identifiers[0]] = row
    metadata_missing_ids = [
        paper_id for paper_id in metadata_ids if paper_id not in metadata_by_id
    ]

    target_references = [
        record
        for paper_id in outgoing_ids
        if (record := _metadata_record(
            metadata_by_id.get(paper_id, {"id": paper_id}),
            relation_role="target_references",
            target_pub_id=target,
            review_as_of=review_as_of,
        ))
    ]
    citing_target: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    candidate_exclusions: list[dict[str, Any]] = []
    same_year_uncertain = sum(
        record["cutoff_status"] == "uncertain_same_year" for record in target_references
    )
    limitations: list[str] = [
        "The public AMiner paper/relation endpoint exposes only references cited "
        "by the target; it does not provide papers that cite the target."
    ]
    if same_year_uncertain:
        limitations.append(
            "Some reference metadata has year-only precision in the cutoff year; "
            "it remains explicitly cutoff-uncertain and requires date verification."
        )
    if errors:
        limitations.append(
            "One or more AMiner relation/metadata requests failed; available backward-reference evidence is incomplete."
        )
    if metadata_missing_ids:
        limitations.append(
            "paper_info returned no metadata for some relation IDs; inspect metadata_missing_ids."
        )
    if successful_queries == 1 and not errors and not metadata_missing_ids:
        status = "completed"
    elif successful_queries or metadata_rows:
        status = "partial"
    else:
        status = "failed"
    return {
        "schema_version": "0.1",
        "provider": "AMiner Open Platform paper/relation + paper_info",
        "target_pub_id": target,
        "review_as_of": review_as_of,
        "status": status,
        "non_fatal": True,
        "queries": query_diagnostics,
        "metadata_status": metadata_status,
        "summary": {
            "total_http_request_count": (
                relation_calls_attempted + paper_info_batches_attempted
            ),
            "relation_api_calls_attempted": relation_calls_attempted,
            "relation_api_calls_completed": successful_queries,
            "paper_info_batches_attempted": paper_info_batches_attempted,
            "paper_info_batches_completed": paper_info_batches_completed,
            "target_reference_ids": len(outgoing_ids),
            "citing_target_ids": 0,
            "metadata_records": len(metadata_rows),
            "candidate_records": len(candidates),
            "same_year_cutoff_uncertain": same_year_uncertain,
        },
        "target_references": target_references,
        "citing_target_records": citing_target,
        "candidate_records": candidates,
        "candidate_exclusions": candidate_exclusions,
        "metadata_missing_ids": metadata_missing_ids,
        "errors": errors,
        "provider_errors": errors,
        "limitations": limitations,
        "supported_directions": ["target_references"],
        "unsupported_directions": ["papers_citing_target"],
        "cost": {
            "currency": "CNY",
            "estimated_total_yuan": round(relation_calls_attempted * 0.10, 2),
            "calls": relation_calls_attempted + paper_info_batches_attempted,
            "breakdown": [
                {
                    "api": "paper_relation",
                    "calls": relation_calls_attempted,
                    "unit_price_yuan": 0.10,
                },
                {
                    "api": "paper_info",
                    "calls": paper_info_batches_attempted,
                    "unit_price_yuan": 0.0,
                },
            ],
        },
        "api_calls": api_calls,
    }


__all__ = [
    "AMINER_PAPER_INFO_URL",
    "AMINER_PAPER_RELATION_URL",
    "fetch_aminer_relation_context",
    "unavailable_relation_context",
]
