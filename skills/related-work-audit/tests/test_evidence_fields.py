"""An equivalent response field cannot supply missing source evidence."""
from copy import deepcopy

import pytest

from related_work_audit.planning import _same_domain_evidence
from test_recall_evidence import PROFILE, candidate, choice, select


MANUSCRIPT = "Sequential recommendation predicts user preferences from long histories."
REASON = "The alternative memory design contextualizes how the manuscript compresses long recommendation histories."


def example():
    row = candidate(relevance_score="0", facet_alignment={
        "profile_available": True, "task_domain_fit": False, "publication_fit": False})
    item = choice(reason=REASON, relation_scope="same_domain", domain_evidence={
        "primary_domain_id": "D1", "primary_domain": "recommendation from interaction histories",
        "manuscript_quote": MANUSCRIPT, "candidate_quote": row["summary"],
    })
    return row, item


@pytest.mark.parametrize("missing", [None, "", " \n "])
def test_existing_reason_recovers_blank_explanation_without_mutating_response(missing):
    row, item = example()
    item["domain_evidence"]["explanation"] = missing
    original = deepcopy(item)
    selected, diagnostics = select([row], [item], MANUSCRIPT)
    assert len(selected) == 1 and selected[0]["tier"] == "extended"
    evidence = selected[0]["same_domain_evidence"]
    assert evidence["evidence"]["explanation"] == REASON
    assert evidence["explanation_source"] == "reason"
    assert all(evidence["checks"].values())
    assert item == original
    assert diagnostics["model_selection_payload"] == {"selected": [original]}


def test_supplied_explanation_is_not_replaced_and_absent_object_is_not_created():
    row, item = example()
    explicit = "Its retrieval component offers an alternative to history compression."
    item["domain_evidence"]["explanation"] = explicit
    selected, _ = select([row], [item], MANUSCRIPT)
    evidence = selected[0]["same_domain_evidence"]
    assert evidence["evidence"]["explanation"] == explicit
    assert evidence["explanation_source"] == "domain_evidence.explanation"
    item.pop("domain_evidence")
    assert not select([row], [item], MANUSCRIPT)[0]


@pytest.mark.parametrize("fault", [
    "invalid_id", "wrong_id_category", "manuscript_quote", "candidate_quote",
    "future_date", "unresolved_date", "already_cited", "missing_identity",
    "self", "low_relevance", "cross_domain", "missing_reason", "nontext_reason",
])
def test_reason_alias_cannot_bypass_evidence_or_eligibility(fault):
    row, item = example()
    if fault == "invalid_id": item["domain_evidence"]["primary_domain_id"] = "D99"
    elif fault == "wrong_id_category": item["domain_evidence"]["primary_domain_id"] = "M1"
    elif fault in {"manuscript_quote", "candidate_quote"}:
        item["domain_evidence"][fault] = "This fabricated passage is absent from the supplied source."
    elif fault == "future_date": row["published"] = "2025-01-01"
    elif fault == "unresolved_date": row["published"] = "2024"
    elif fault == "already_cited": row["missing_status"] = "already_cited"
    elif fault == "missing_identity": row.update(aminer_pub_id="", url="")
    elif fault == "self": row["title"] = "A New Recommendation Study"
    elif fault == "low_relevance": item["relevance"] = "low"
    elif fault == "cross_domain": item["relation_scope"] = "cross_domain_transfer"
    elif fault == "missing_reason": item["reason"] = ""
    elif fault == "nontext_reason": item["reason"] = {"text": REASON}
    assert not select([row], [item], MANUSCRIPT)[0]


def test_legacy_profile_labels_keep_their_existing_complete_label_requirement():
    row, item = example()
    item["domain_evidence"].pop("primary_domain_id")
    passed, _ = _same_domain_evidence(item, row, PROFILE, MANUSCRIPT, "A New Recommendation Study")
    assert not passed
    item["domain_evidence"]["primary_domain"] = "sequential recommendation"
    passed, evidence = _same_domain_evidence(item, row, PROFILE, MANUSCRIPT, "A New Recommendation Study")
    assert passed and evidence["explanation_source"] == "reason"
