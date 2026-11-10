"""Conservative coverage matching between retrieved work and a paper bibliography.

This module answers only whether a retrieved candidate can be matched to the
uploaded manuscript's structured reference ledger. It does not decide that an
unmatched candidate was wrongly omitted.
"""

from __future__ import annotations

from .limits import EVALUATION_VIEW_LIMIT

import re
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlparse


DOI_PATTERN = re.compile(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", re.IGNORECASE)
ARXIV_PATTERN = re.compile(r"(?:arxiv\.org/(?:abs|pdf)/|arxiv\s*:\s*)(\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE)


def _normal_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _normal_doi(value: Any) -> str:
    match = DOI_PATTERN.search(str(value or ""))
    return match.group(0).rstrip(".,;)").lower() if match else ""


def _normal_arxiv(value: Any) -> str:
    value = str(value or "").strip()
    match = ARXIV_PATTERN.search(value)
    if match:
        return match.group(1).lower()
    return re.sub(r"v\d+$", "", value.lower()) if re.fullmatch(r"\d{4}\.\d{4,5}(?:v\d+)?", value, re.I) else ""


def _candidate_identifiers(candidate: dict[str, Any]) -> tuple[str, str]:
    url = str(candidate.get("url") or "")
    doi = _normal_doi(candidate.get("doi")) or _normal_doi(url)
    arxiv_id = _normal_arxiv(candidate.get("arxiv_id")) or _normal_arxiv(url)
    return doi, arxiv_id


def _best_title_match(candidate_title: str, references: list[dict[str, Any]]) -> tuple[float, dict[str, Any] | None]:
    normalized = _normal_text(candidate_title)
    if len(normalized.split()) < 4:
        return 0.0, None
    scored = [
        (
            SequenceMatcher(
                None,
                normalized,
                _normal_text(
                    reference.get("candidate_title") or reference.get("title")
                ),
            ).ratio(),
            reference,
        )
        for reference in references
        if _normal_text(reference.get("candidate_title") or reference.get("title"))
    ]
    return max(scored, default=(0.0, None), key=lambda item: item[0])


def _reference_evidence(reference: dict[str, Any], match_type: str, score: float | None = None) -> dict[str, Any]:
    evidence = {
        "reference_id": reference.get("id"),
        "reference_title": reference.get("candidate_title"),
        "match_type": match_type,
        "anchor": reference.get("anchor"),
    }
    if score is not None:
        evidence["title_similarity"] = round(score, 3)
    return evidence


def _relation_evidence(
    reference: dict[str, Any],
    match_type: str,
    score: float | None = None,
) -> dict[str, Any]:
    evidence = {
        "reference_id": reference.get("aminer_pub_id"),
        "reference_title": reference.get("title"),
        "match_type": match_type,
        "provider": "AMiner Open Platform paper/relation",
        "relation": "target_cites_candidate",
        "relation_evidence": reference.get("relation_evidence"),
    }
    if score is not None:
        evidence["title_similarity"] = round(score, 3)
    return evidence


def assess_related_work_coverage(
    candidates: list[dict[str, Any]],
    reference_ledger: dict[str, Any] | None,
    *,
    relation_references: list[dict[str, Any]] | None = None,
    covered_title_threshold: float = 0.92,
    ambiguous_title_threshold: float = 0.78,
) -> dict[str, Any]:
    """Match time-safe retrieved candidates to a structured bibliography ledger.

    ``candidate_to_check`` means a retrieved candidate was not matched. It is a
    review queue item, never a claim that an author omitted a required citation.
    """
    ledger = reference_ledger or {}
    references = [
        item for item in ledger.get("records", [])
        if isinstance(item, dict) and item.get("resolution_eligible", True)
    ]
    ledger_ready = ledger.get("status") == "ready_for_resolution" and bool(references)
    relation_references = [
        item for item in (relation_references or [])
        if isinstance(item, dict)
    ]
    two_tier_enabled = any(
        isinstance(item, dict)
        and any(
            item.get(field) is not None
            for field in ("tier", "confidence", "candidate_label")
        )
        for item in candidates
    )
    relation_ready = bool(relation_references)
    limitations: list[str] = []
    if not ledger_ready:
        if relation_ready:
            limitations.append(
                "The local bibliography ledger is unavailable or did not pass its "
                "parse-quality gate. AMiner target-reference relations are discovery "
                "hints and cannot establish citations in this manuscript version."
            )
        else:
            limitations.append("Reference coverage was not assessed because the local bibliography ledger is unavailable or did not pass its parse-quality gate.")

    doi_index = {_normal_doi(item.get("doi")): item for item in references if _normal_doi(item.get("doi"))}
    arxiv_index = {_normal_arxiv(item.get("arxiv_id")): item for item in references if _normal_arxiv(item.get("arxiv_id"))}
    relation_id_index = {
        str(item.get("aminer_pub_id") or "").lower(): item
        for item in relation_references
        if item.get("aminer_pub_id")
    }
    records: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates, start=1):
        title = str(candidate.get("title") or "").strip()
        doi, arxiv_id = _candidate_identifiers(candidate)
        record = {
            "candidate_id": str(candidate.get("candidate_id") or f"C{index}"),
            "title": title,
            "authors": candidate.get("authors"),
            "published": candidate.get("published"),
            "source": candidate.get("source"),
            "url": candidate.get("url"),
            "summary": candidate.get("summary"),
            "selection_reason": candidate.get("selection_reason"),
            "retrieval_role": candidate.get("retrieval_role"),
            "comparison_role": candidate.get("comparison_role"),
            "status": "not_assessed",
            "match": None,
            "reason": "",
        }
        for field in (
            "tier",
            "confidence",
            "candidate_label",
            "missing_status",
            "semantic_relevance",
            "selection_source",
            "fill_source",
            "selection_evidence",
            "relation_scope",
            "transfer_evidence",
            "same_domain_evidence",
            "publication_date_evidence",
            "publication_year_conflict",
            "same_title_versions",
            "selection_candidate_id",
            "selection_batch",
            "selection_batch_rank",
            "verification",
            "cross_round_retention",
        ):
            if candidate.get(field) is not None:
                record[field] = candidate.get(field)
        for field in (
            "aminer_pub_id",
            "relation_role",
            "relation_evidence",
            "date_precision",
            "cutoff_status",
            "time_admissibility",
        ):
            if candidate.get(field) is not None:
                record[field] = candidate.get(field)
        relation_pub_id = str(candidate.get("aminer_pub_id") or "").lower()
        relation_match = relation_id_index.get(relation_pub_id)
        relation_score, relation_title_match = _best_title_match(
            title, relation_references
        )
        if relation_match:
            record["external_reference_hint"] = _relation_evidence(
                relation_match, "aminer_paper_relation_id"
            )
        elif relation_title_match and relation_score >= covered_title_threshold:
            record["external_reference_hint"] = _relation_evidence(
                relation_title_match,
                "aminer_paper_relation_title_high_similarity",
                relation_score,
            )
        if not ledger_ready:
            record["reason"] = limitations[0]
        elif doi and doi in doi_index:
            record["status"] = "covered"
            record["match"] = _reference_evidence(doi_index[doi], "doi")
            record["reason"] = "The candidate DOI exactly matches a parsed bibliography entry."
        elif arxiv_id and arxiv_id in arxiv_index:
            record["status"] = "covered"
            record["match"] = _reference_evidence(arxiv_index[arxiv_id], "arxiv_id")
            record["reason"] = "The candidate arXiv identifier exactly matches a parsed bibliography entry."
        else:
            score, matched = _best_title_match(title, references)
            if matched and score >= covered_title_threshold:
                record["status"] = "covered"
                record["match"] = _reference_evidence(matched, "title_high_similarity", score)
                record["reason"] = "The candidate title has a high-similarity match in the parsed bibliography."
            elif matched and score >= ambiguous_title_threshold:
                record["status"] = "ambiguous_match"
                record["match"] = _reference_evidence(matched, "title_partial_similarity", score)
                record["reason"] = "A partial title match was found; human review is needed before treating it as covered."
            else:
                record["status"] = "candidate_to_check"
                record["reason"] = "No exact identifier or high-confidence title match was found. This is a review candidate, not an omission finding."
        if two_tier_enabled:
            if record["status"] == "covered":
                record["missing_status"] = "already_cited"
            elif record["status"] == "candidate_to_check" and ledger_ready:
                record["missing_status"] = "uncited"
            else:
                record["missing_status"] = "unknown"
            verification = record.get("verification")
            verification = dict(verification) if isinstance(verification, dict) else {}
            verification["bibliography"] = {
                "coverage_status": record["status"],
                "missing_status": record["missing_status"],
                "match": record["match"],
                "interpretation": (
                    "No parsed bibliography match was found; this remains a "
                    "candidate_to_check and is not proof of an omission."
                    if record["missing_status"] == "uncited"
                    else "The candidate matched the parsed bibliography of this manuscript."
                    if record["missing_status"] == "already_cited"
                    else "Bibliography coverage could not be resolved confidently."
                ),
            }
            record["verification"] = verification
        records.append(record)

    assessed_records = records
    already_cited_controls: list[dict[str, Any]] = []
    final_exclusions: list[dict[str, Any]] = []
    if two_tier_enabled:
        already_cited_controls = [
            {
                **item,
                "control_status": "already_cited",
                "final_output_eligible": False,
                "control_uses": ["retrieval_seed", "coverage_control"],
            }
            for item in assessed_records
            if item.get("missing_status") == "already_cited"
        ]
        final_records: list[dict[str, Any]] = []
        for item in assessed_records:
            missing_status = str(item.get("missing_status") or "unknown")
            tier = str(item.get("tier") or "")
            if missing_status == "already_cited":
                continue
            if (
                item.get("time_admissibility") == "uncertain"
                or item.get("cutoff_status") == "uncertain_same_year"
            ):
                final_exclusions.append(
                    {
                        "candidate_id": item.get("candidate_id"),
                        "title": item.get("title"),
                        "tier": tier,
                        "missing_status": missing_status,
                        "reason": "cutoff_date_unresolved",
                    }
                )
                continue
            if tier == "core" and missing_status != "uncited":
                final_exclusions.append(
                    {
                        "candidate_id": item.get("candidate_id"),
                        "title": item.get("title"),
                        "tier": tier,
                        "missing_status": missing_status,
                        "reason": "core_requires_bibliography_confirmed_uncited",
                    }
                )
                continue
            # Extended candidates may remain citation-status unknown for human
            # verification, but an already-cited work was removed above.
            final_records.append(item)
        final_records.sort(key=lambda item: item.get("tier") != "core")
        for item in final_records[EVALUATION_VIEW_LIMIT:]:
            final_exclusions.append({"candidate_id": item.get("candidate_id"), "title": item.get("title"), "reason": "output_cap"})
        records = final_records[:EVALUATION_VIEW_LIMIT]

    counts = {
        status: sum(item["status"] == status for item in records)
        for status in (
            "covered",
            "ambiguous_match",
            "candidate_to_check",
            "not_assessed",
        )
    }
    summary: dict[str, Any] = {"candidates_checked": len(assessed_records), **counts}
    if two_tier_enabled:
        assessed_counts = {
            status: sum(item["status"] == status for item in assessed_records)
            for status in (
                "covered",
                "ambiguous_match",
                "candidate_to_check",
                "not_assessed",
            )
        }
        summary.update(
            {
                "assessed_statuses": assessed_counts,
                "final_candidate_count": len(records),
                "cross_domain_transfer_count": sum(item.get("relation_scope") == "cross_domain_transfer" for item in records),
                "candidate_tiers": {
                    tier: sum(item.get("tier") == tier for item in records)
                    for tier in ("core", "extended")
                },
                "missing_statuses": {
                    status: sum(
                        item.get("missing_status") == status for item in records
                    )
                    for status in ("uncited", "already_cited", "unknown")
                },
                "already_cited_control_count": len(already_cited_controls),
                "core_unknown_excluded_count": sum(
                    item.get("reason")
                    == "core_requires_bibliography_confirmed_uncited"
                    for item in final_exclusions
                ),
                "cutoff_unresolved_excluded_count": sum(
                    item.get("reason") == "cutoff_date_unresolved"
                    for item in final_exclusions
                ),
                "partition_conservation": {
                    "assessed": len(assessed_records),
                    "final_candidates": len(records),
                    "already_cited_controls": len(already_cited_controls),
                    "other_final_exclusions": len(final_exclusions),
                    "conserved": len(assessed_records)
                    == len(records)
                    + len(already_cited_controls)
                    + len(final_exclusions),
                },
                "evaluation_view_limit": EVALUATION_VIEW_LIMIT,
                "evaluation_view_status": (
                    "at_cap" if len(records) == EVALUATION_VIEW_LIMIT else "insufficient_candidates"
                ),
            }
        )
    result = {
        "schema_version": "0.1",
        "status": (
            "completed"
            if ledger_ready
            else "partial"
            if relation_ready
            else "not_assessed"
        ),
        "summary": summary,
        "records": records,
        "limitations": limitations,
    }
    if two_tier_enabled:
        result["already_cited_controls"] = already_cited_controls
        result["final_selection_exclusions"] = final_exclusions
    return result
