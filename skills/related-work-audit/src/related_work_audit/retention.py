"""Keep verified selections across feedback rounds without another model call."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import re
from typing import Any

from .coverage import assess_related_work_coverage
from .evidence import verified_quote
from .limits import CANDIDATE_EXCERPT_CHARS, EVALUATION_VIEW_LIMIT
from .planning import _medium_confidence_gate
from .publication_time import date_bounds
from .retrieval import filter_retrieval_records, reference_title_similarity


def _title_key(row: dict[str, Any]) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(row.get("title") or "").casefold()).strip()


def _same_identity(left: dict[str, Any], right: dict[str, Any]) -> bool:
    a, b = (str(row.get("aminer_pub_id") or "").casefold() for row in (left, right))
    if a and b:
        return a == b
    return bool(_title_key(left)) and _title_key(left) == _title_key(right)


def _same_work(left: dict[str, Any], right: dict[str, Any]) -> bool:
    # Separate AMiner versions may share a title. Deduplicate their outputs,
    # but never attach the metadata of one stable ID to another.
    return _same_identity(left, right) or bool(_title_key(left)) and _title_key(left) == _title_key(right)


def _grounded_snapshot(row: dict[str, Any], field: str) -> bool:
    evidence = row.get(field) or {}
    quote = " ".join(str(evidence.get("evidence", {}).get("candidate_quote") or "").casefold().split())
    supplied = " ".join((str(row.get("title") or "") + " " +
                         str(row.get("summary") or "")[:CANDIDATE_EXCERPT_CHARS]).casefold().split())
    return bool(evidence.get("passed") and verified_quote(quote, supplied))


def _revalidate(
    original: dict[str, Any], updates: list[dict[str, Any]], *,
    local_title: str, review_as_of: str, manuscript_profile: dict[str, Any],
    local_references: list[dict[str, Any]], bibliography_ready: bool,
    cited_controls: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    prior_gate = (original.get("verification") or {}).get("relevance") or {}
    if not (prior_gate.get("passed") and prior_gate.get("checks")
            and all(prior_gate["checks"].values())
            and original.get("tier") in {"core", "extended"}
            and str(original.get("selection_reason") or "").strip()):
        return None, {"reason": "no_prior_verified_selection"}

    row = deepcopy(original)
    changed = []
    for update in updates:
        if not _same_identity(original, update):
            continue
        if update.get("title") and reference_title_similarity(original["title"], update["title"]) < 0.92:
            return None, {"reason": "source_identity_conflict", "source_record": update}
        if (original.get("arxiv_id") and update.get("arxiv_id")
                and re.sub(r"v\d+$", "", str(original["arxiv_id"]))
                != re.sub(r"v\d+$", "", str(update["arxiv_id"]))):
            return None, {"reason": "source_identity_conflict", "source_record": update}
        if (update.get("final_output_eligible") is False
                and update.get("relation_role") not in {"known_cited_control", "target_references", "target_reference_hint"}):
            return None, {"reason": "new_source_ineligibility", "source_record": update}
        # A shorter/new abstract is not a refutation of an earlier source quote.
        # Keep the original relevance evidence; refresh factual date metadata.
        published = update.get("published")
        old_bounds, new_bounds = date_bounds(row.get("published")), date_bounds(published)
        less_precise = bool(old_bounds and new_bounds and
                            new_bounds[0] <= old_bounds[0] <= old_bounds[1] <= new_bounds[1])
        if published and published != row.get("published") and not less_precise:
            changed.append({"field": "published", "before": row.get("published"),
                            "after": published, "source": update.get("source"),
                            "aminer_pub_id": update.get("aminer_pub_id")})
            row["published"] = published
        for field in ("doi", "arxiv_id", "publication_date_evidence"):
            if update.get(field) and not row.get(field):
                row[field] = deepcopy(update[field])
    for field in ("date_precision", "cutoff_status", "time_admissibility",
                  "local_citation_match", "local_citation_ambiguity"):
        row.pop(field, None)
    filtered, diagnostics = filter_retrieval_records(
        [row], local_title=local_title, review_as_of=review_as_of,
        manuscript_profile=manuscript_profile, local_references=local_references,
        bibliography_ready=bibliography_ready,
    )
    detail = {"source_updates": changed,
              "filter_decisions": diagnostics["filter_decisions"]}
    if not filtered:
        return None, {**detail, "reason": "source_boundary_failed"}
    row = filtered[0]
    domain_ok = _grounded_snapshot(original, "same_domain_evidence")
    transfer_ok = _grounded_snapshot(original, "transfer_evidence")
    passed, gate = _medium_confidence_gate(
        row, local_title=local_title, review_as_of=review_as_of,
        grounded_same_domain=domain_ok, grounded_transfer=transfer_ok,
    )
    if row.get("relation_scope") == "cross_domain_transfer" and not transfer_ok:
        passed = False
        gate["passed"] = False
        gate["failed_checks"].append("grounded_transfer_required")
    detail["revalidated_gate"] = gate
    if not passed:
        return None, {**detail, "reason": "evidence_gate_failed"}
    coverage = assess_related_work_coverage(
        [row], {"status": "ready_for_resolution" if bibliography_ready else "unready",
                "records": local_references}, relation_references=cited_controls,
    )
    if not coverage["records"]:
        return None, {**detail, "reason": "bibliography_or_final_eligibility_failed",
                      "coverage_summary": coverage["summary"],
                      "already_cited_controls": coverage.get("already_cited_controls", []),
                      "final_exclusions": coverage.get("final_selection_exclusions", [])}
    checked = coverage["records"][0]
    row["missing_status"] = checked["missing_status"]
    row["verification"]["bibliography"] = checked["verification"]["bibliography"]
    row["verification"]["date"] = {key: row.get(key) for key in
        ("published", "date_precision", "cutoff_status", "time_admissibility")}
    return row, {**detail, "reason": "verified_evidence_still_admissible"}


def merge_verified_rounds(
    previous: list[dict[str, Any]], current: list[dict[str, Any]], *,
    new_source_records: list[dict[str, Any]], local_title: str,
    review_as_of: str, manuscript_profile: dict[str, Any],
    local_references: list[dict[str, Any]] | None = None,
    bibliography_ready: bool = False,
    cited_controls: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Union only previously verified selections, rechecking hard boundaries.

    Core comes first; within a tier the earlier verified order takes priority.
    Every cap eviction and duplicate remains visible in the decision ledger.
    Round-local selector IDs are never treated as publication identities.
    """
    qualified = []
    decisions = []
    for round_number, rows in ((1, previous), (2, current)):
        for rank, original in enumerate(rows, 1):
            row, detail = _revalidate(
                original, new_source_records, local_title=local_title,
                review_as_of=review_as_of, manuscript_profile=manuscript_profile,
                local_references=local_references or [], bibliography_ready=bibliography_ready,
                cited_controls=cited_controls or [],
            )
            decision = {"round": round_number, "rank": rank, "title": original.get("title"),
                        "aminer_pub_id": original.get("aminer_pub_id"),
                        "selection_candidate_id": original.get("selection_candidate_id"),
                        "disposition": "qualified" if row else "revalidation_excluded", **detail}
            decisions.append(decision)
            if row is None:
                continue
            row["cross_round_retention"] = {
                "selected_round": round_number, "original_rank": rank,
                "selection_candidate_id": original.get("selection_candidate_id"),
                "preserved_source_evidence": deepcopy(original.get("selection_evidence")),
                "revalidation": detail,
            }
            qualified.append((row, decision))
    qualified.sort(key=lambda pair: pair[0]["tier"] != "core")
    selected = []
    for row, decision in qualified:
        match = next((item for item in selected if _same_work(item, row)), None)
        if match is not None:
            decision.update(disposition="duplicate_selected_work", represented_by={
                "aminer_pub_id": match.get("aminer_pub_id"), "title": match.get("title")})
        elif len(selected) >= EVALUATION_VIEW_LIMIT:
            decision.update(disposition="output_cap")
        else:
            added = decision["round"] == 1 and not any(_same_work(row, item) for item in current)
            decision.update(disposition="retained_previous_round" if added else "selected")
            row["cross_round_retention"]["added_by_retention"] = added
            selected.append(row)
    return selected, {
        "status": "completed", "policy": "verified_union; recheck_source_and_bibliography; core_first; earlier_round_first_within_tier; cap_30",
        "previous_selected_count": len(previous), "current_selected_count": len(current),
        "final_selected_count": len(selected),
        "added_by_retention_count": sum(row["cross_round_retention"]["added_by_retention"] for row in selected),
        "decisions": decisions, "decision_counts": dict(Counter(d["disposition"] for d in decisions)),
        "output_limit": EVALUATION_VIEW_LIMIT, "extra_model_calls": 0, "extra_search_calls": 0,
    }
