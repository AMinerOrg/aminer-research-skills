"""Provider adapters and deterministic policy for scholarly retrieval."""

from __future__ import annotations

import inspect
import re
import time
from collections import Counter
from datetime import date
from difflib import SequenceMatcher
from typing import Any, Callable

import httpx

from .aminer_search import retrieve_aminer_records
from .limits import PRESELECTION_LIMIT
from .publication_time import arxiv_month_evidence, date_bounds, earliest_supported_time
from .sources import arxiv_search, openalex_abstract, openalex_search
from .rw_coverage import related_work_coverage


RETRIEVAL_QUERY_ROLES = (
    "problem_setting",
    "method_mechanism",
    "named_prior_work",
    "direct_baseline",
    "benchmark_or_evaluation",
    "alternative_formulation",
    "related_finding",
)
RETRIEVAL_STOPWORDS = {
    "a", "an", "and", "as", "at", "based", "by", "for", "from", "in",
    "large", "language", "method", "methods", "model", "models", "of", "on",
    "or", "paper", "review", "study", "the", "to", "using", "with", "multi",
    "modal", "multimodal",
}
RETRIEVAL_GENERIC_TOKENS = {
    "analysis", "benchmark", "benchmarks", "comparison", "comparisons", "dataset",
    "datasets", "encoder", "encoders", "evaluation", "evaluations", "feature",
    "features", "framework", "learning", "reasoning", "representation",
    "representations", "task", "tasks", "vision", "visual",
}
PROFILE_FIELDS = (
    "domain_terms",
    "task_terms",
    "mechanism_terms",
    "data_protocol_terms",
)

SelectionFunction = Callable[..., tuple[list[dict[str, str]], dict[str, Any]]]
FeedbackFunction = Callable[..., tuple[list[Any], dict[str, Any]]]


def _supports_keyword_budget(function: Callable[..., Any]) -> bool:
    try:
        parameters = inspect.signature(function).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        or parameter.name == "allow_keyword_expansion"
        for parameter in parameters
    )


def _supports_argument(function: Callable[..., Any], name: str) -> bool:
    try:
        parameters = inspect.signature(function).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        or parameter.name == name
        for parameter in parameters
    )


def _keyword_expansion_used(diagnostics: dict[str, Any]) -> bool:
    return any(
        isinstance(call, dict)
        and call.get("api") == "paper_list_by_keywords"
        and str(call.get("status") or "")
        in {"completed", "completed_empty", "completed_low_quality"}
        for call in diagnostics.get("api_calls", [])
    )


def _merge_provider_diagnostics(
    first: dict[str, Any],
    second: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(first)
    for key in ("api_calls", "downgrades", "provider_errors", "provider_warnings"):
        merged[key] = list(first.get(key) or []) + list(second.get(key) or [])
    first_cost = first.get("cost") if isinstance(first.get("cost"), dict) else {}
    second_cost = second.get("cost") if isinstance(second.get("cost"), dict) else {}
    merged["cost"] = {
        "currency": "CNY",
        "estimated_total_yuan": round(
            float(first_cost.get("estimated_total_yuan") or 0.0)
            + float(second_cost.get("estimated_total_yuan") or 0.0),
            4,
        ),
        "calls": int(first_cost.get("calls") or 0)
        + int(second_cost.get("calls") or 0),
        "note": "Aggregated across at most two controlled retrieval rounds.",
    }
    merged["raw_record_count"] = int(first.get("raw_record_count") or 0) + int(
        second.get("raw_record_count") or 0
    )
    merged["feedback_round"] = second
    return merged


def reference_title_similarity(left: str, right: str) -> float:
    normalize = lambda value: re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    return SequenceMatcher(None, normalize(left), normalize(right)).ratio()




def parse_iso_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def meaningful_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.lower())
        if len(token) >= 3 and token not in RETRIEVAL_STOPWORDS
    }


def manuscript_profile_from_queries(queries: list[Any]) -> dict[str, list[str]]:
    """Recover the manuscript retrieval facets carried by routed queries.

    Older callers only provide strings, so roles are also used as a conservative
    fallback.  The profile is manuscript-derived; it never contains a benchmark
    title or a domain-specific rule baked into the Skill.
    """
    profile: dict[str, list[str]] = {field: [] for field in PROFILE_FIELDS}
    fallback_by_field: dict[str, list[str]] = {
        field: [] for field in PROFILE_FIELDS
    }
    role_field = {
        "problem_setting": "task_terms",
        "method_mechanism": "mechanism_terms",
        "benchmark_or_evaluation": "data_protocol_terms",
    }
    for item in queries:
        if isinstance(item, dict):
            embedded = item.get("retrieval_profile")
            query = str(item.get("query") or item.get("text") or "").strip()
            role = str(item.get("role") or "").strip()
        else:
            embedded = getattr(item, "retrieval_profile", None)
            query = str(item or "").strip()
            role = str(getattr(item, "role", "") or "").strip()
        if isinstance(embedded, dict):
            for field in PROFILE_FIELDS:
                values = embedded.get(field)
                if isinstance(values, list):
                    profile[field].extend(
                        str(value).strip()
                        for value in values
                        if str(value).strip()
                    )
        fallback_field = role_field.get(role)
        if fallback_field and query:
            fallback_by_field[fallback_field].append(query)
    for field in PROFILE_FIELDS:
        if not profile[field]:
            profile[field].extend(fallback_by_field[field])
        profile[field] = list(
            dict.fromkeys(" ".join(value.split()) for value in profile[field])
        )[:8]
    if not profile["domain_terms"] and profile["task_terms"]:
        # Older planners did not emit a separate domain axis. Reuse the
        # manuscript-derived task phrases under the stricter coherent-overlap
        # matcher so incidental generic words cannot establish domain fit.
        profile["domain_terms"] = list(profile["task_terms"])
    return profile


def _phrase_tokens(value: str) -> set[str]:
    return meaningful_tokens(value).difference(RETRIEVAL_GENERIC_TOKENS)


def _matched_profile_terms(text: str, terms: list[str]) -> list[str]:
    candidate_tokens = _phrase_tokens(text)
    matched: list[str] = []
    for term in terms:
        term_tokens = _phrase_tokens(term)
        if not term_tokens:
            continue
        overlap = len(candidate_tokens.intersection(term_tokens))
        threshold = 1 if len(term_tokens) == 1 else 2
        if overlap >= threshold:
            matched.append(term)
    return matched


def _matched_domain_terms(text: str, terms: list[str]) -> list[str]:
    """Require coherent domain-phrase overlap, not two incidental task words."""
    candidate_tokens = _phrase_tokens(text)
    matched: list[str] = []
    for term in terms:
        term_tokens = _phrase_tokens(term)
        if not term_tokens:
            continue
        overlap = len(candidate_tokens.intersection(term_tokens))
        threshold = max(2, (3 * len(term_tokens) + 4) // 5)
        if overlap >= threshold:
            matched.append(term)
    return matched


def coherent_domain_score(record: dict[str, Any], profile: dict[str, list[str]]) -> int:
    """Ranking hint: preserve domain qualifiers and require nearby word overlap.

    The ordinary lexical gate drops words such as language/model and can confuse
    unrelated senses of memory or agent. This hint never excludes a candidate.
    """
    def words(value: str) -> list[str]:
        value = re.sub(r"\bllms?\b", "large language model", value.casefold())
        tokens = re.findall(r"[a-z0-9]+", value)
        return [word[:-1] if len(word) > 4 and word.endswith("s") else word
                for word in tokens if word not in {"a", "an", "the", "of", "in", "on", "for", "and", "with"}]
    text = words(" ".join(str(record.get(field) or "") for field in ("title", "summary")))
    hits = 0
    for term in profile.get("domain_terms", []):
        required = set(words(term))
        if len(required) < 2:
            continue
        threshold = max(2, (4 * len(required) + 4) // 5)
        width = len(required) + 4
        if any(len(required.intersection(text[start:start+width])) >= threshold
               for start in range(len(text))):
            hits += 1
    return hits


def facet_alignment(
    record: dict[str, Any],
    profile: dict[str, list[str]],
) -> dict[str, Any]:
    """Measure task/mechanism/protocol agreement without domain hard-coding."""
    text = " ".join(
        str(record.get(field) or "")
        for field in ("title", "summary", "venue")
    )
    matched = {
        field: (
            _matched_domain_terms(text, profile.get(field, []))
            if field == "domain_terms"
            else _matched_profile_terms(text, profile.get(field, []))
        )
        for field in PROFILE_FIELDS
    }
    domain_required = bool(profile.get("domain_terms"))
    domain = bool(matched["domain_terms"])
    task = bool(matched["task_terms"])
    mechanism = bool(matched["mechanism_terms"])
    protocol = bool(matched["data_protocol_terms"])
    local_citation = bool(record.get("local_citation_match"))
    named_query = (
        str(record.get("query_role") or record.get("retrieval_role") or "")
        in {"named_prior_work", "direct_baseline"}
        and str(record.get("query_shape") or "")
        in {"exact_title", "title_phrase"}
    )
    # Explicit domain evidence supports related tasks, mechanisms, or protocols.
    # Without a domain profile, task evidence remains the conservative fallback.
    task_domain_fit = domain if domain_required else task
    publication_fit = (
        task_domain_fit and (task or mechanism or protocol)
    ) or (named_query and local_citation)
    cross_domain_task = domain_required and not domain and bool(
        task or mechanism or protocol
    )
    axes = [
        name
        for name, present in (
            ("domain", domain),
            ("task", task),
            ("mechanism", mechanism),
            ("data_protocol", protocol),
        )
        if present
    ]
    score = (
        7 * int(domain)
        + 5 * int(task)
        + 4 * int(mechanism)
        + 3 * int(protocol)
    )
    score += min(sum(len(values) for values in matched.values()), 4)
    score += 6 * int(local_citation)
    return {
        "profile_available": any(profile.get(field) for field in PROFILE_FIELDS),
        "domain_required": domain_required,
        "domain_aligned": domain,
        "task_domain_fit": task_domain_fit,
        "task_aligned": task,
        "mechanism_aligned": mechanism,
        "data_protocol_aligned": protocol,
        "aligned_axes": axes,
        "matched_profile_terms": matched,
        "local_citation_supported": local_citation,
        "named_work_query": named_query,
        "publication_fit": publication_fit,
        "cross_domain_task": cross_domain_task,
        "mechanism_only_cross_domain": cross_domain_task
        or (mechanism and not task_domain_fit),
        "score": score,
    }


def technical_coverage_summary(
    selected: list[dict[str, Any]],
    profile: dict[str, list[str]],
    queries: list[Any] | None = None,
) -> dict[str, Any]:
    """Describe verifiable Core coverage of manuscript-derived components."""
    expected = list(profile.get("mechanism_terms") or [])
    core: list[dict[str, Any]] = []
    for item in selected:
        if str(item.get("tier") or "") != "core":
            continue
        if str(item.get("missing_status") or "") != "uncited":
            continue
        verification = item.get("verification")
        relevance = (
            verification.get("relevance")
            if isinstance(verification, dict)
            else None
        )
        if isinstance(relevance, dict) and relevance.get("passed") is False:
            continue
        core.append(item)
    covered: list[str] = []
    for term in expected:
        if any(
            term
            in (
                (item.get("facet_alignment") or {}).get(
                    "matched_profile_terms", {}
                ).get("mechanism_terms", [])
                if isinstance(item.get("facet_alignment"), dict)
                else []
            )
            for item in core
        ):
            covered.append(term)
    missing = [term for term in expected if term not in covered]
    expected_named: list[str] = []
    for query in queries or []:
        if isinstance(query, dict):
            value = str(query.get("query") or query.get("text") or "").strip()
            role = str(query.get("role") or "").strip()
        else:
            value = str(query or "").strip()
            role = str(getattr(query, "role", "") or "").strip()
        if value and role in {"named_prior_work", "direct_baseline"}:
            expected_named.append(value)
    expected_named = list(dict.fromkeys(expected_named))
    covered_named = []
    for query in expected_named:
        normalized = " ".join(query.casefold().split())
        if any(
            " ".join(str(item.get("query") or "").casefold().split())
            == normalized
            for item in core
        ):
            covered_named.append(query)
    missing_named = [query for query in expected_named if query not in covered_named]
    cross_axis_core = sum(
        bool((item.get("facet_alignment") or {}).get("task_aligned"))
        and (
            bool((item.get("facet_alignment") or {}).get("mechanism_aligned"))
            or bool(
                (item.get("facet_alignment") or {}).get(
                    "data_protocol_aligned"
                )
            )
        )
        for item in core
        if isinstance(item.get("facet_alignment"), dict)
    )
    ratio = len(covered) / len(expected) if expected else None
    named_ratio = (
        len(covered_named) / len(expected_named) if expected_named else None
    )
    return {
        "expected_mechanism_terms": expected,
        "covered_mechanism_terms": covered,
        "missing_mechanism_terms": missing,
        "mechanism_coverage_ratio": ratio,
        "expected_named_queries": expected_named,
        "covered_named_queries": covered_named,
        "missing_named_queries": missing_named,
        "named_query_coverage_ratio": named_ratio,
        "verified_core_count": len(core),
        "cross_axis_core_count": cross_axis_core,
        "sufficient": bool(core)
        and (ratio is None or ratio >= 0.75)
        and (named_ratio is None or named_ratio >= 0.75)
        and (cross_axis_core >= 1 or not profile.get("task_terms")),
    }


def is_same_paper_title(candidate_title: str, local_title: str) -> bool:
    candidate = meaningful_tokens(candidate_title)
    local = meaningful_tokens(local_title)
    if not candidate or not local:
        return False
    overlap = len(candidate.intersection(local)) / min(len(candidate), len(local))
    return overlap >= 0.8 or reference_title_similarity(
        candidate_title,
        local_title,
    ) >= 0.9


def retrieval_relevance_score(
    record: dict[str, str],
    local_title: str,
) -> tuple[int, bool]:
    record_title_tokens = meaningful_tokens(str(record.get("title") or ""))
    record_summary_tokens = meaningful_tokens(str(record.get("summary") or ""))
    local_tokens = meaningful_tokens(local_title)
    query_tokens = meaningful_tokens(str(record.get("query") or ""))
    distinctive_tokens = (local_tokens.union(query_tokens)).difference(
        RETRIEVAL_GENERIC_TOKENS
    )
    title_overlap = len(record_title_tokens.intersection(local_tokens))
    query_overlap = len(record_title_tokens.intersection(query_tokens))
    summary_overlap = len(
        record_summary_tokens.intersection(local_tokens.union(query_tokens))
    )
    has_distinctive_title_overlap = bool(
        record_title_tokens.intersection(distinctive_tokens)
    )
    return (
        title_overlap * 3 + query_overlap * 2 + min(summary_overlap, 2),
        has_distinctive_title_overlap,
    )


def _normal_doi(value: Any) -> str:
    match = re.search(
        r"10\.\d{4,9}/[-._;()/:a-z0-9]+",
        str(value or ""),
        flags=re.I,
    )
    return match.group(0).rstrip(".,;)").casefold() if match else ""


def _normal_arxiv(value: Any) -> str:
    match = re.search(
        r"(?:arxiv\.org/(?:abs|pdf)/|arxiv\s*:\s*)?(\d{4}\.\d{4,5})(?:v\d+)?",
        str(value or ""),
        flags=re.I,
    )
    return match.group(1).casefold() if match else ""


def _local_citation_match(
    candidate: dict[str, Any],
    local_references: list[dict[str, Any]],
) -> dict[str, Any] | None:
    candidate_title = str(candidate.get("title") or "")
    candidate_doi = _normal_doi(candidate.get("doi")) or _normal_doi(
        candidate.get("url")
    )
    candidate_arxiv = _normal_arxiv(candidate.get("arxiv_id")) or _normal_arxiv(
        candidate.get("url")
    )
    best: tuple[float, dict[str, Any]] | None = None
    for reference in local_references:
        reference_doi = _normal_doi(reference.get("doi"))
        reference_arxiv = _normal_arxiv(reference.get("arxiv_id"))
        if candidate_doi and candidate_doi == reference_doi:
            return {
                "reference_id": reference.get("reference_id") or reference.get("id"),
                "reference_title": (
                    reference.get("candidate_title")
                    or reference.get("title")
                    or reference.get("reference_title")
                ),
                "match_type": "doi",
                "coverage_status": "already_cited",
                "evidence_source": "parsed_local_bibliography",
            }
        if candidate_arxiv and candidate_arxiv == reference_arxiv:
            return {
                "reference_id": reference.get("reference_id") or reference.get("id"),
                "reference_title": (
                    reference.get("candidate_title")
                    or reference.get("title")
                    or reference.get("reference_title")
                ),
                "match_type": "arxiv_id",
                "coverage_status": "already_cited",
                "evidence_source": "parsed_local_bibliography",
            }
        reference_title = str(
            reference.get("candidate_title")
            or reference.get("title")
            or reference.get("reference_title")
            or ""
        ).strip()
        if not reference_title:
            continue
        similarity = reference_title_similarity(candidate_title, reference_title)
        if similarity < 0.78:
            continue
        if best is None or similarity > best[0]:
            best = (similarity, reference)
    if best is None:
        return None
    similarity, reference = best
    return {
        "reference_id": reference.get("reference_id") or reference.get("id"),
        "reference_title": (
            reference.get("candidate_title")
            or reference.get("title")
            or reference.get("reference_title")
        ),
        "title_similarity": round(similarity, 4),
        "match_type": (
            "title_high_similarity" if similarity >= 0.92 else "title_ambiguous"
        ),
        "coverage_status": (
            "already_cited" if similarity >= 0.92 else "ambiguous_match"
        ),
        "evidence_source": "parsed_local_bibliography",
    }


def _partition_already_cited_controls(
    records: list[dict[str, Any]],
    *,
    bibliography_ready: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Separate bibliography matches from candidates that may be missing.

    Only matches against a ready local bibliography establish known citations.
    External target-reference edges may belong to another manuscript version.
    Keeping the split deterministic prevents a relevance model from promoting
    an already-cited paper into the final missing-related-work view.
    """
    candidates: list[dict[str, Any]] = []
    controls: list[dict[str, Any]] = []
    for record in records:
        match = record.get("local_citation_match")
        confirmed = (bibliography_ready and isinstance(match, dict)
                     and match.get("coverage_status") == "already_cited"
                     and match.get("evidence_source") == "parsed_local_bibliography")
        if not confirmed:
            candidate = dict(record)
            candidate["missing_status"] = (
                "unknown"
                if isinstance(record.get("local_citation_ambiguity"), dict)
                or not bibliography_ready
                else "uncited"
            )
            candidates.append(candidate)
            continue
        controls.append(
            {
                "title": record.get("title"),
                "authors": record.get("authors"),
                "published": record.get("published"),
                "summary": record.get("summary"),
                "source": record.get("source"),
                "url": record.get("url"),
                "aminer_pub_id": record.get("aminer_pub_id"),
                "retrieval_query": record.get("query"),
                "retrieval_role": record.get("retrieval_role"),
                "facet_alignment": record.get("facet_alignment"),
                "missing_status": "already_cited",
                "control_uses": ["retrieval_seed", "coverage_control"],
                "bibliography_match": match,
                "relation_evidence": record.get("relation_evidence"),
                "final_output_eligible": False,
                "exclusion_reason": (
                    "The parsed bibliography of this manuscript confirms "
                    "that this work is already cited; use it only to guide "
                    "adjacent-work discovery and coverage diagnosis."
                ),
            }
        )
    return candidates, controls


def partition_relation_references(
    records: list[dict[str, Any]], *, local_references: list[dict[str, Any]],
    bibliography_ready: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Recheck external reference edges against the actual manuscript ledger."""
    references = [r for r in local_references if r.get("resolution_eligible", True)]
    prepared = []
    for source in records:
        record = dict(source)
        for field in ("local_citation_match", "local_citation_ambiguity", "bibliography_match",
                      "missing_status", "final_output_eligible", "exclusion_reason",
                      "cutoff_status", "time_admissibility", "date_evidence"):
            record.pop(field, None)
        record["relation_role"] = "target_reference_hint"
        record["retrieval_role"] = "citation_graph_reference"
        record["external_reference_hint"] = {
            "relation_evidence": source.get("relation_evidence"),
            "interpretation": "External graph edge; citation in this input version requires local bibliography evidence.",
        }
        match = _local_citation_match(record, references)
        if bibliography_ready and match:
            field = "local_citation_match" if match["coverage_status"] == "already_cited" else "local_citation_ambiguity"
            record[field] = match
        prepared.append(record)
    return _partition_already_cited_controls(prepared, bibliography_ready=bibliography_ready)


def _control_titles(controls: list[dict[str, Any]]) -> list[str]:
    return list(
        dict.fromkeys(
            " ".join(str(item.get("title") or "").split())
            for item in controls
            if str(item.get("title") or "").strip()
        )
    )


def _merge_controls(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            key = str(item.get("aminer_pub_id") or "").casefold().strip()
            if not key:
                key = " ".join(str(item.get("title") or "").casefold().split())
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


def _exclude_known_control_matches(
    records: list[dict[str, Any]],
    controls: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Remove search hits already proven cited before the Top-40 boundary."""
    remaining: list[dict[str, Any]] = []
    matches: list[dict[str, Any]] = []
    for record in records:
        record_id = str(record.get("aminer_pub_id") or "").casefold().strip()
        best_title_match: tuple[float, dict[str, Any]] | None = None
        exact_id_control: dict[str, Any] | None = None
        for control in controls:
            control_id = str(control.get("aminer_pub_id") or "").casefold().strip()
            if record_id and control_id:
                if record_id == control_id:
                    exact_id_control = control
                    break
                # Conflicting stable IDs override a coincidentally similar title.
                continue
            similarity = reference_title_similarity(
                str(record.get("title") or ""),
                str(control.get("title") or ""),
            )
            if similarity >= 0.92 and (
                best_title_match is None or similarity > best_title_match[0]
            ):
                best_title_match = (similarity, control)
        if exact_id_control is not None:
            matches.append(
                {
                    "title": record.get("title"),
                    "aminer_pub_id": record.get("aminer_pub_id"),
                    "retrieval_query": record.get("query"),
                    "retrieval_role": record.get("retrieval_role")
                    or record.get("query_role"),
                    "match_type": "aminer_pub_id",
                    "matched_control_title": exact_id_control.get("title"),
                    "matched_control_pub_id": exact_id_control.get("aminer_pub_id"),
                }
            )
            continue
        if best_title_match is not None:
            similarity, control = best_title_match
            matches.append(
                {
                    "title": record.get("title"),
                    "aminer_pub_id": record.get("aminer_pub_id"),
                    "retrieval_query": record.get("query"),
                    "retrieval_role": record.get("retrieval_role")
                    or record.get("query_role"),
                    "match_type": "title_high_similarity_without_shared_id",
                    "title_similarity": round(similarity, 4),
                    "matched_control_title": control.get("title"),
                    "matched_control_pub_id": control.get("aminer_pub_id"),
                }
            )
            continue
        remaining.append(record)
    return remaining, matches


def _version_preference(item: dict[str, Any], cutoff: date | None) -> tuple[Any, ...]:
    value = str(item.get("published") or "").strip()
    bounds = date_bounds(value)
    if not bounds:
        rank = 3
    elif cutoff and bounds[0] > cutoff:
        rank = 2
    elif cutoff and bounds[1] > cutoff:
        rank = 1
    else:
        rank = 0
    return rank, value[:4], -len(value), value, -len(str(item.get("summary") or ""))


def filter_retrieval_records(
    records: list[dict[str, str]],
    *,
    local_title: str,
    review_as_of: str,
    max_records: int = PRESELECTION_LIMIT,
    manuscript_profile: dict[str, list[str]] | None = None,
    local_references: list[dict[str, Any]] | None = None,
    bibliography_ready: bool = False,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Apply source, cutoff, self-match, and deduplication boundaries."""
    cutoff = parse_iso_date(review_as_of)
    diagnostics: dict[str, Any] = {
        "review_as_of": review_as_of,
        "local_title_detected": bool(local_title),
        "raw_record_count": len(records),
        "duplicate_records_removed": 0,
        "self_records_removed": 0,
        "future_records_removed": 0,
        "undated_records_removed": 0,
        "low_relevance_records_removed": 0,
        "generic_only_records_removed": 0,
    }
    profile = manuscript_profile or {field: [] for field in PROFILE_FIELDS}
    reference_pool = [
        item
        for item in (local_references or [])
        if isinstance(item, dict) and item.get("resolution_eligible", True)
    ]
    references = reference_pool if bibliography_ready else []
    advanced_facets = bool(references) or any(
        profile.get(field) for field in PROFILE_FIELDS
    )
    if reference_pool:
        diagnostics.update(
            local_citation_supported_records=0,
            local_citation_provisional_seed_records=0,
        )
    if advanced_facets:
        diagnostics["mechanism_only_cross_domain_records"] = 0
    seen: set[str] = set()
    ranked: list[tuple[int, dict[str, str]]] = []
    # Prefer an admissible representative before deduplication. Keep each
    # version's own metadata; never attach an older date to a newer paper ID.
    groups: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    decisions = []
    for index, item in enumerate(records):
        derived = arxiv_month_evidence(item, "retrieved_record_identifier")
        if derived:
            item = dict(item)
            evidence = list(item.get("publication_date_evidence") or [])
            if date_bounds(item.get("published")):
                evidence.append({"source": "retrieved_record", "field": "published",
                                 "value": str(item["published"])})
            for value in derived:
                if value not in evidence:
                    evidence.append(value)
            item["publication_date_evidence"] = evidence
            item["published"] = earliest_supported_time(evidence)
        key = " ".join(str(item.get("title") or "").lower().split())
        groups.setdefault(key, []).append((index, item))
        decisions.append({"record_index": index, "title": item.get("title"), "published": item.get("published"), "aminer_pub_id": item.get("aminer_pub_id"), "disposition": "pending"})
    ordered = []
    for versions in groups.values():
        versions = sorted(versions, key=lambda pair: _version_preference(pair[1], cutoff))
        index, representative = versions[0]
        if len(versions) > 1:
            representative = dict(representative)
            representative["same_title_versions"] = [
                {key: value.get(key) for key in ("aminer_pub_id", "published", "source", "query")}
                for _, value in versions
            ]
        ordered.extend([(index, representative), *versions[1:]])
    for record_index, item in ordered:
        decision = decisions[record_index]
        title = str(item.get("title") or "").strip()
        key = " ".join(title.lower().split())
        if not title or key in seen:
            diagnostics["duplicate_records_removed"] += 1
            decision["disposition"] = "duplicate_or_empty_title"
            continue
        seen.add(key)
        if local_title and is_same_paper_title(title, local_title):
            diagnostics["self_records_removed"] += 1
            decision["disposition"] = "self"
            continue
        local_citation_evidence = _local_citation_match(item, reference_pool)
        local_citation_match = (
            local_citation_evidence
            if bibliography_ready
            and isinstance(local_citation_evidence, dict)
            and local_citation_evidence.get("coverage_status") == "already_cited"
            else None
        )
        local_citation_ambiguity = (
            local_citation_evidence
            if bibliography_ready
            and isinstance(local_citation_evidence, dict)
            and local_citation_evidence.get("coverage_status") == "ambiguous_match"
            else None
        )
        local_citation_provisional_seed = (
            local_citation_evidence
            if not bibliography_ready and isinstance(local_citation_evidence, dict)
            else None
        )
        if local_citation_match:
            item = dict(item)
            item["local_citation_match"] = local_citation_match
            diagnostics["local_citation_supported_records"] += 1
        elif local_citation_ambiguity:
            item = dict(item)
            item["local_citation_ambiguity"] = local_citation_ambiguity
            diagnostics["local_citation_ambiguous_records"] = (
                diagnostics.get("local_citation_ambiguous_records", 0) + 1
            )
        elif local_citation_provisional_seed:
            item = dict(item)
            item["local_citation_provisional_seed"] = {
                **local_citation_provisional_seed,
                "coverage_status": "provisional_untrusted_ledger",
            }
            diagnostics["local_citation_provisional_seed_records"] = (
                diagnostics.get("local_citation_provisional_seed_records", 0) + 1
            )
        published_raw = str(item.get("published") or "").strip()
        month_only = bool(re.fullmatch(r"\d{4}-\d{2}", published_raw))
        year_only = bool(re.fullmatch(r"\d{4}", published_raw)) and int(published_raw) > 0
        if cutoff and month_only and date_bounds(published_raw):
            lower, upper = date_bounds(published_raw)
            if lower > cutoff:
                diagnostics["future_records_removed"] += 1
                decision["disposition"] = "metadata_after_cutoff"
                continue
            item = dict(item)
            item["date_precision"] = "month"
            if upper <= cutoff:
                item["cutoff_status"] = "admissible_month_before_cutoff"
                item["time_admissibility"] = "admissible"
            elif local_citation_match:
                item["cutoff_status"] = "admissible_via_local_citation"
                item["time_admissibility"] = "admissible"
            else:
                item["cutoff_status"] = "uncertain_cutoff_month"
                item["time_admissibility"] = "uncertain"
                diagnostics["same_month_date_uncertain_records"] = diagnostics.get("same_month_date_uncertain_records", 0) + 1
        elif cutoff and year_only:
            published_year = int(published_raw)
            if published_year > cutoff.year:
                diagnostics["future_records_removed"] += 1
                decision["disposition"] = "metadata_after_cutoff"
                continue
            if published_year == cutoff.year and date_bounds(published_raw)[1] > cutoff:
                diagnostics["same_year_date_uncertain_records"] = (
                    diagnostics.get("same_year_date_uncertain_records", 0) + 1
                )
                # AMiner commonly returns year precision only.  Keep the record
                # visible, but never pretend that January 1 is its publication
                # date or claim it was definitely available before an intra-year
                # review cutoff.
                item = dict(item)
                item["date_precision"] = "year"
                if local_citation_match:
                    item["cutoff_status"] = "admissible_via_local_citation"
                    item["time_admissibility"] = "admissible"
                    item["date_evidence"] = local_citation_match
                else:
                    item["cutoff_status"] = "uncertain_same_year"
                    item["time_admissibility"] = "uncertain"
            else:
                item = dict(item)
                item["date_precision"] = "year"
                item["cutoff_status"] = (
                    "admissible_year_through_cutoff" if published_year == cutoff.year
                    else "admissible_year_before_cutoff"
                )
                item["time_admissibility"] = "admissible"
        else:
            published = parse_iso_date(published_raw)
            if cutoff and not published:
                diagnostics["undated_records_removed"] += 1
                decision["disposition"] = "undated_or_invalid_date"
                continue
            if cutoff and published and published > cutoff:
                diagnostics["future_records_removed"] += 1
                decision["disposition"] = "metadata_after_cutoff"
                continue
            if cutoff and published and (
                "aminer" in str(item.get("source") or "").lower()
                or item.get("date_precision") is not None
            ):
                item = dict(item)
                item["date_precision"] = "day"
                item["cutoff_status"] = "admissible_exact_date"
                item["time_admissibility"] = "admissible"
        relevance, distinctive = retrieval_relevance_score(item, local_title)
        entry = dict(item)
        entry["retrieval_record_index"] = record_index
        decision["disposition"] = "preselection_cap"
        entry["relevance_score"] = str(relevance)
        entry["has_distinctive_title_overlap"] = str(distinctive).lower()
        if advanced_facets:
            alignment = facet_alignment(entry, profile)
            entry["facet_alignment"] = alignment
            entry["facet_alignment_score"] = str(alignment["score"])
            if alignment["mechanism_only_cross_domain"]:
                diagnostics["mechanism_only_cross_domain_records"] += 1
            relevance += int(alignment["score"])
            entry["coherent_domain_score"] = coherent_domain_score(entry, profile)
        ranked.append((relevance, entry))
    ranked.sort(key=lambda pair: (pair[1].get("coherent_domain_score", 0) > 0,
                                  pair[0]), reverse=True)
    # A global top-N can let one broad query crowd out every exact/named branch.
    # Reserve the strongest qualified hit for each independent named query,
    # then fill the remaining budget by the global task/facet score.
    named_representatives: list[tuple[int, dict[str, Any]]] = []
    represented_queries: set[str] = set()
    for score, item in ranked:
        role = str(item.get("query_role") or item.get("retrieval_role") or "")
        query = " ".join(str(item.get("query") or "").casefold().split())
        if (
            not advanced_facets
            or role not in {"named_prior_work", "direct_baseline"}
            or not query
        ):
            continue
        if query in represented_queries:
            continue
        named_representatives.append((score, item))
        represented_queries.add(query)
    # Preserve independent discovery routes instead of letting a large noisy
    # query consume all 80 slots. Already-cited controls travel outside this
    # discovery budget and are partitioned by the caller as before.
    controls = [item for _, item in ranked if item.get("local_citation_match")]
    buckets: dict[str, list[dict[str, Any]]] = {}
    for _, item in ranked:
        if item.get("local_citation_match"):
            continue
        key = " ".join(str(item.get("query") or item.get("retrieval_role") or "unclassified").casefold().split())
        buckets.setdefault(key, []).append(item)
    discovery_accepted = []
    while any(buckets.values()) and len(discovery_accepted) < max_records:
        for bucket in buckets.values():
            if bucket and len(discovery_accepted) < max_records:
                discovery_accepted.append(bucket.pop(0))
    accepted = discovery_accepted + controls
    if advanced_facets:
        diagnostics["named_query_representative_count"] = len(
            named_representatives[:max_records]
        )
        diagnostics["named_queries_represented"] = sorted(represented_queries)
    diagnostics["candidate_record_count"] = len(seen)
    for item in accepted:
        decision = decisions[item["retrieval_record_index"]]
        decision["disposition"] = "kept"
        decision["cutoff_status"] = item.get("cutoff_status")
    diagnostics["preselection_limit"] = max_records
    diagnostics["preselection_discovery_count"] = len(discovery_accepted)
    diagnostics["preselection_controls_outside_budget"] = len(controls)
    diagnostics["preselection_query_group_count"] = len(buckets)
    diagnostics["preselection_policy"] = "coherent_domain_hint_within_query; round_robin_discovery_queries; cited_controls_outside_budget"
    diagnostics["preselection_cap_removed_count"] = len(ranked) - len(accepted)
    diagnostics["filter_decisions"] = decisions
    diagnostics["kept_record_count"] = len(accepted)
    diagnostics["status"] = (
        "candidates_ready" if accepted else "no_admissible_records"
    )
    diagnostics["policy"] = (
        "Publication evidence must fall within the supplied boundary. A year-end "
        "boundary includes year-only records from that year; an overlapping "
        "intra-year window remains uncertain. Candidate precision is preserved. "
        "Self matches are excluded. Lexical relevance is a ranking hint; final "
        "selection uses source-grounded task, mechanism and evaluation relationships."
    )
    return accepted, diagnostics



def retrieve_related_work(
    queries: list[Any],
    *,
    local_title: str,
    review_as_of: str,
    proxy: str | None,
    select_relevant: SelectionFunction,
    plan_feedback: FeedbackFunction | None = None,
    preselect_candidates: Callable[..., Any] | None = None,
    seed_records: list[dict[str, Any]] | None = None,
    known_cited_controls: list[dict[str, Any]] | None = None,
    local_references: list[dict[str, Any]] | None = None,
    bibliography_ready: bool = False,
    provider: str = "aminer",
    aminer_api_key: str | None = None,
    aminer_search_tier: str = "auto",
    aminer_detail_limit: int = 5,
    prefer_openalex: bool = False,
    client_factory: Callable[..., Any] = httpx.Client,
    sleep: Callable[[float], None] = time.sleep,
    arxiv_search_fn: Callable[..., list[dict[str, str]]] = arxiv_search,
    openalex_search_fn: Callable[..., list[dict[str, str]]] = openalex_search,
    aminer_search_fn: Callable[..., tuple[list[dict[str, Any]], dict[str, Any]]] = retrieve_aminer_records,
    trace: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    pool_statuses = []
    def filter_with_trace(records, **kwargs):
        filter_kwargs = dict(kwargs)
        if preselect_candidates is not None:
            filter_kwargs['max_records'] = len(records)
        filtered, diagnostics = filter_retrieval_records(records, **filter_kwargs)
        if preselect_candidates is not None:
            discovery, controls = _partition_already_cited_controls(
                filtered, bibliography_ready=bool(kwargs.get('bibliography_ready')),
            )
            chosen, pool_diag = preselect_candidates(
                discovery, local_title=kwargs['local_title'], review_as_of=kwargs['review_as_of'],
                manuscript_profile=kwargs.get('manuscript_profile') or {},
            )
            pool_statuses.append(pool_diag.get('status','unknown'))
            chosen_indices = {r['retrieval_record_index'] for r in chosen}
            # Controls have their own compact schema, so recover their source
            # positions before replacing the filtered rows with that schema.
            discovery_indices = {r['retrieval_record_index'] for r in discovery}
            control_indices = {r['retrieval_record_index'] for r in filtered
                               if r['retrieval_record_index'] not in discovery_indices}
            bank_indices = set(pool_diag.get('bank_record_indices',[]))
            pending_indices = set(pool_diag.get('pending_record_indices',[]))
            for decision in diagnostics['filter_decisions']:
                if decision['disposition'] != 'kept':
                    continue
                index = decision['record_index']
                if index in control_indices:
                    decision['disposition'] = 'known_cited_control'
                elif index not in chosen_indices:
                    decision['disposition'] = ('pending_publication_date' if index in pending_indices else
                        'semantic_preselection_not_returned' if index in bank_indices else 'semantic_bank_cap')
            # The caller partitions once more; preserve the original citation
            # evidence instead of feeding compact controls back as discoveries.
            filtered = chosen + [r for r in filtered if r['retrieval_record_index'] in control_indices]
            diagnostics.update(candidate_pool_preselection=pool_diag,
                preselection_limit=pool_diag.get('shortlist_limit'),
                preselection_discovery_count=len(chosen),
                preselection_cap_removed_count=pool_diag.get('bank_cap_omitted',0),
                semantic_preselection_not_returned_count=max(0,pool_diag.get('bank_count',0)-len(chosen)),
                kept_record_count=len(filtered),
                preselection_policy='bounded_semantic_corpus_shortlist; cited_controls_outside_budget')
        if trace:
            trace({"event": "retrieval_filter", "raw_records": records,
                   "filtered_records": filtered, "diagnostics": diagnostics})
        return filtered, diagnostics

    # v0.3 callers may still pass this explicit flag. Treat it as an explicit
    # provider choice; it is not an automatic AMiner failure fallback.
    if prefer_openalex and provider == "aminer":
        provider = "openalex"
    relation_candidates, explicit_cited_controls = partition_relation_references(
        known_cited_controls or [], local_references=local_references or [],
        bibliography_ready=bibliography_ready,
    )
    results: list[dict[str, str]] = relation_candidates + [dict(item) for item in (seed_records or [])]
    selection_queries: list[Any] = list(queries)
    manuscript_profile = manuscript_profile_from_queries(selection_queries)
    errors: list[str] = []
    provider_diagnostics: dict[str, Any] = {
        "provider": provider,
        "provider_errors": [],
    }
    if provider == "aminer":
        if not str(aminer_api_key or "").strip():
            raise ValueError(
                "AMINER_API_KEY is required for the default AMiner search provider. "
                "Choose an explicit external --search-provider to avoid AMiner."
            )
        aminer_arguments: dict[str, Any] = {
            "review_as_of": review_as_of,
            "api_key": str(aminer_api_key),
            "proxy": proxy,
            "search_tier": aminer_search_tier,
            "detail_limit": aminer_detail_limit,
            "client_factory": client_factory,
        }
        if _supports_keyword_budget(aminer_search_fn):
            aminer_arguments["allow_keyword_expansion"] = True
        aminer_records, provider_diagnostics = aminer_search_fn(
            queries,
            **aminer_arguments,
        )
        results.extend(dict(item) for item in aminer_records)
        if trace:
            trace({"event": "aminer_search_round", "round": 1, "records": aminer_records, "diagnostics": provider_diagnostics})
        errors.extend(
            str(error)
            for error in provider_diagnostics.get("provider_errors", [])
            if str(error).strip()
        )
    elif provider in {"arxiv", "openalex", "arxiv-openalex"}:
        with client_factory(proxy=proxy, timeout=25, follow_redirects=True) as client:
            if provider in {"arxiv", "arxiv-openalex"}:
                for raw_query in queries:
                    query = str(raw_query.get("query") or "").strip() if isinstance(raw_query, dict) else str(raw_query).strip()
                    if not query:
                        continue
                    try:
                        results.extend(arxiv_search_fn(query, client))
                    except Exception as exc:
                        errors.append(f"arXiv: {type(exc).__name__}")
                    sleep(3)
            if provider == "openalex":
                for raw_query in queries:
                    query = str(raw_query.get("query") or "").strip() if isinstance(raw_query, dict) else str(raw_query).strip()
                    if not query:
                        continue
                    try:
                        results.extend(
                            openalex_search_fn(
                                query, client, review_as_of=review_as_of
                            )
                        )
                    except TypeError:
                        # Preserve compatibility with small injected test doubles.
                        results.extend(openalex_search_fn(query, client))
                    except Exception as exc:
                        errors.append(f"OpenAlex: {type(exc).__name__}")
        provider_diagnostics = {
            "provider": provider,
            "external_fallback_enabled": provider == "arxiv-openalex",
            "external_fallback_used": False,
            "api_calls": [],
            "cost": {
                "currency": "CNY",
                "estimated_total_yuan": 0.0,
                "calls": None,
                "note": "arXiv/OpenAlex are explicit external providers, not an AMiner fallback.",
            },
        }
    else:
        raise ValueError(f"Unknown retrieval provider: {provider}")

    query_texts = [
        str(query.get("query") or "").strip() if isinstance(query, dict) else str(query).strip()
        for query in queries
    ]
    query_roles = {
        text: (
            str(queries[index].get("role") or "").strip()
            if isinstance(queries[index], dict) and queries[index].get("role")
            else str(getattr(queries[index], "role", "") or "").strip()
            if getattr(queries[index], "role", None)
            else RETRIEVAL_QUERY_ROLES[index]
            if index < len(RETRIEVAL_QUERY_ROLES)
            else "unclassified"
        )
        for index, text in enumerate(query_texts)
        if text
    }
    for record in results:
        record.setdefault(
            "retrieval_role",
            query_roles.get(
                str(record.get("query") or ""),
                str(record.get("query_role") or "unclassified"),
            ),
        )
    discovery_results, known_control_matches = _exclude_known_control_matches(
        results,
        explicit_cited_controls,
    )
    filtered, diagnostics = filter_with_trace(
        discovery_results,
        local_title=local_title,
        review_as_of=review_as_of,
        manuscript_profile=manuscript_profile,
        local_references=local_references,
        bibliography_ready=bibliography_ready,
    )
    discovery_candidates, retrieved_cited_controls = _partition_already_cited_controls(
        filtered,
        bibliography_ready=bibliography_ready,
    )
    cited_controls = _merge_controls(
        explicit_cited_controls,
        retrieved_cited_controls,
    )
    selected, selection_diagnostics = select_relevant(
        discovery_candidates,
        local_title=local_title,
        queries=selection_queries,
        review_as_of=review_as_of,
    )
    technical_coverage = technical_coverage_summary(
        selected, manuscript_profile, selection_queries
    )
    rw_coverage = related_work_coverage(selected, selection_queries)
    technical_coverage["related_work"] = rw_coverage
    feedback_diagnostics: dict[str, Any] = {
        "rounds_completed": 1,
        "maximum_rounds": 2,
        "status": "not_needed",
        "stop_reason": "initial_coverage_sufficient",
        "queries": [],
    }
    core_count = sum(
        item.get("tier") == "core"
        and item.get("missing_status") == "uncited"
        for item in selected
    )
    feedback_needed = (
        provider == "aminer"
        and plan_feedback is not None
        and bool(queries)
        and (
            (not rw_coverage["sufficient"])
            if rw_coverage.get("status") == "assessed"
            else (core_count < 4 or not bool(technical_coverage.get("sufficient")))
        )
    )
    if feedback_needed:
        feedback_arguments: dict[str, Any] = {
            "original_queries": selection_queries,
            "review_as_of": review_as_of,
            "technical_coverage": technical_coverage,
        }
        if _supports_argument(plan_feedback, "already_cited_controls"):
            feedback_arguments["already_cited_controls"] = cited_controls
        feedback_queries, feedback_plan = plan_feedback(
            filtered,
            selected,
            **feedback_arguments,
        )
        feedback_diagnostics.update(feedback_plan)
        feedback_diagnostics["queries"] = [str(item) for item in feedback_queries]
        if feedback_queries:
            previous_selected = selected
            previous_selection_diagnostics = selection_diagnostics
            second_arguments: dict[str, Any] = {
                "review_as_of": review_as_of,
                "api_key": str(aminer_api_key),
                "proxy": proxy,
                "search_tier": aminer_search_tier,
                "detail_limit": aminer_detail_limit,
                "client_factory": client_factory,
            }
            if _supports_keyword_budget(aminer_search_fn):
                second_arguments["allow_keyword_expansion"] = not _keyword_expansion_used(
                    provider_diagnostics
                )
            second_records, second_diagnostics = aminer_search_fn(
                feedback_queries,
                **second_arguments,
            )
            if trace:
                trace({"event": "aminer_search_round", "round": 2, "records": second_records, "diagnostics": second_diagnostics})
            provider_diagnostics = _merge_provider_diagnostics(
                provider_diagnostics,
                second_diagnostics,
            )
            errors.extend(
                str(error)
                for error in second_diagnostics.get("provider_errors", [])
                if str(error).strip()
            )
            feedback_query_texts = [str(item).strip() for item in feedback_queries]
            query_texts.extend(feedback_query_texts)
            selection_queries.extend(feedback_queries)
            feedback_roles = {
                text: str(getattr(item, "role", "") or "unclassified")
                for text, item in zip(feedback_query_texts, feedback_queries)
            }
            query_roles.update(feedback_roles)
            results.extend(dict(item) for item in second_records)
            for record in results:
                record.setdefault(
                    "retrieval_role",
                    query_roles.get(
                        str(record.get("query") or ""),
                        str(record.get("query_role") or "unclassified"),
                    ),
                )
            discovery_results, known_control_matches = (
                _exclude_known_control_matches(
                    results,
                    explicit_cited_controls,
                )
            )
            filtered, diagnostics = filter_with_trace(
                discovery_results,
                local_title=local_title,
                review_as_of=review_as_of,
                manuscript_profile=manuscript_profile,
                local_references=local_references,
                bibliography_ready=bibliography_ready,
            )
            discovery_candidates, retrieved_cited_controls = (
                _partition_already_cited_controls(
                    filtered,
                    bibliography_ready=bibliography_ready,
                )
            )
            cited_controls = _merge_controls(
                explicit_cited_controls,
                retrieved_cited_controls,
            )
            selected, selection_diagnostics = select_relevant(
                discovery_candidates,
                local_title=local_title,
                queries=selection_queries,
                review_as_of=review_as_of,
            )
            # Import here because the evidence checker also uses retrieval helpers.
            from .retention import merge_verified_rounds

            current_selection_diagnostics = selection_diagnostics
            selected, retention_diagnostics = merge_verified_rounds(
                previous_selected, selected, new_source_records=second_records,
                local_title=local_title, review_as_of=review_as_of,
                manuscript_profile=manuscript_profile,
                local_references=local_references, bibliography_ready=bibliography_ready,
                cited_controls=cited_controls,
            )
            selection_diagnostics = dict(current_selection_diagnostics)
            selection_diagnostics.update(
                cross_round_retention=retention_diagnostics,
                selection_rounds=[previous_selection_diagnostics, current_selection_diagnostics],
                selected_record_count=len(selected),
                core_record_count=sum(row.get("tier") == "core" for row in selected),
                extended_record_count=sum(row.get("tier") == "extended" for row in selected),
                cross_domain_transfer_count=sum(row.get("relation_scope") == "cross_domain_transfer" for row in selected),
                selected_roles=dict(Counter(row.get("comparison_role") for row in selected)),
                evaluation_view_status="at_cap" if len(selected) == retention_diagnostics["output_limit"] else "insufficient_candidates",
                final_selection_policy=retention_diagnostics["policy"],
                candidate_decisions_scope="last_model_round_before_verified_union; see cross_round_retention.decisions for final dispositions",
            )
            if any(str(d.get("selection_status", "")).startswith("failed_")
                   or d.get("selection_status") == "partial"
                   for d in (previous_selection_diagnostics, current_selection_diagnostics)):
                selection_diagnostics["selection_status"] = "partial"
            if trace:
                trace({"event": "cross_round_retention", "selected": selected,
                       "diagnostics": retention_diagnostics})
            technical_coverage = technical_coverage_summary(
                selected, manuscript_profile, selection_queries
            )
            technical_coverage["related_work"] = related_work_coverage(selected, selection_queries)
            feedback_diagnostics.update(
                rounds_completed=2,
                status="completed",
                stop_reason="maximum_rounds_reached_after_verified_union",
                added_raw_records=len(second_records),
            )
        else:
            feedback_diagnostics.update(
                status="no_new_query" if feedback_diagnostics.get("status") == "no_new_query" else "not_needed",
                stop_reason="no_distinct_queries_within_budget" if feedback_diagnostics.get("status") == "no_new_query" else "planner_returned_no_missing_categories",
            )
    if not selected and provider == "arxiv-openalex":
        provider_diagnostics["external_fallback_used"] = True
        with client_factory(proxy=proxy, timeout=25, follow_redirects=True) as client:
            for raw_query in queries:
                query = str(raw_query.get("query") or "").strip() if isinstance(raw_query, dict) else str(raw_query).strip()
                if not query:
                    continue
                try:
                    try:
                        results.extend(
                            openalex_search_fn(
                                query, client, review_as_of=review_as_of
                            )
                        )
                    except TypeError:
                        results.extend(openalex_search_fn(query, client))
                except Exception as exc:
                    errors.append(f"OpenAlex: {type(exc).__name__}")
        for record in results:
            record.setdefault(
                "retrieval_role",
                query_roles.get(
                    str(record.get("query") or ""),
                    str(record.get("query_role") or "unclassified"),
                ),
            )
        discovery_results, known_control_matches = _exclude_known_control_matches(
            results,
            explicit_cited_controls,
        )
        filtered, diagnostics = filter_with_trace(
            discovery_results,
            local_title=local_title,
            review_as_of=review_as_of,
            manuscript_profile=manuscript_profile,
            local_references=local_references,
            bibliography_ready=bibliography_ready,
        )
        discovery_candidates, retrieved_cited_controls = _partition_already_cited_controls(
            filtered,
            bibliography_ready=bibliography_ready,
        )
        cited_controls = _merge_controls(
            explicit_cited_controls,
            retrieved_cited_controls,
        )
        selected, selection_diagnostics = select_relevant(
            discovery_candidates,
            local_title=local_title,
            queries=selection_queries,
            review_as_of=review_as_of,
        )
    diagnostics["provider_errors"] = errors
    diagnostics["retrieval_provider"] = provider
    diagnostics["provider_diagnostics"] = provider_diagnostics
    diagnostics["provider_cost"] = provider_diagnostics.get("cost")
    diagnostics["provider_downgrades"] = provider_diagnostics.get("downgrades", [])
    diagnostics["external_fallback_used"] = bool(
        provider_diagnostics.get("external_fallback_used")
    )
    diagnostics["feedback_retrieval"] = feedback_diagnostics
    diagnostics["already_cited_controls"] = cited_controls
    diagnostics["external_reference_hint_count"] = len(relation_candidates)
    diagnostics["citation_evidence_policy"] = "current_manuscript_bibliography_only"
    diagnostics["already_cited_control_count"] = len(cited_controls)
    diagnostics["already_cited_control_titles"] = _control_titles(cited_controls)
    diagnostics["already_cited_control_policy"] = (
        "Exact or near-title local bibliography matches are retrieval seeds and "
        "coverage controls only. They are excluded before semantic selection "
        "and can never enter the missing-related-work evaluation view."
    )
    diagnostics["known_control_discovery_match_count"] = len(
        known_control_matches
    )
    diagnostics["known_control_discovery_matches"] = known_control_matches
    diagnostics["manuscript_retrieval_profile"] = manuscript_profile
    diagnostics["technical_coverage"] = technical_coverage
    diagnostics["related_work_coverage"] = technical_coverage.get("related_work", {})
    diagnostics["coverage_profile_diagnostics"] = getattr(queries[0], "coverage_profile_diagnostics", {}) if queries else {}
    diagnostics.update(selection_diagnostics)
    diagnostics["kept_record_count"] = len(selected)
    diagnostics["status"] = (
        "partial_selection" if selection_diagnostics.get('selection_status') == 'partial'
        or any(status in {'failed_fallback','partial_invalid_ids'} for status in pool_statuses)
        else "passed" if selected else "no_semantically_relevant_records"
    )
    if pool_statuses:
        diagnostics['candidate_pool_round_statuses'] = pool_statuses
    return selected, diagnostics


__all__ = [
    "RETRIEVAL_QUERY_ROLES",
    "facet_alignment",
    "filter_retrieval_records",
    "manuscript_profile_from_queries",
    "retrieve_related_work",
    "technical_coverage_summary",
]
