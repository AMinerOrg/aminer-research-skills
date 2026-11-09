"""A facet's retrieval category is separate from its transfer use."""
from copy import deepcopy

import pytest

from related_work_audit.planning import _transfer_evidence


MANUSCRIPT = "We use bootstrap confidence intervals to compare benchmark scores."
CANDIDATE = {"title": "Significance Tests for Retrieval Evaluation",
             "summary": "Bootstrap and permutation tests compare retrieval systems on shared queries."}
PROFILE = {"domain_terms": ["language model evaluation"],
           "task_terms": ["benchmark score comparison"],
           "mechanism_terms": ["bootstrap confidence intervals"],
           "data_protocol_terms": ["paired benchmark comparisons"]}
TRANSFER = {"basis": "evaluation", "manuscript_facet_id": "M1",
            "manuscript_facet": "bootstrap confidence intervals",
            "manuscript_quote": MANUSCRIPT, "candidate_quote": CANDIDATE["summary"],
            "explanation": "The shared bootstrap procedure informs uncertainty estimates for score comparisons.",
            "limitation": "Retrieval systems use query-level results, not language-model pretraining checkpoints."}


def verify(transfer):
    return _transfer_evidence({"transfer": transfer}, CANDIDATE, PROFILE, MANUSCRIPT)


@pytest.mark.parametrize("anchor_id,basis,field", [
    ("M1", "evaluation", "mechanism_terms"),
    ("E1", "mechanism", "data_protocol_terms"),
    ("T1", "evaluation", "task_terms"),
    ("T1", "task", "task_terms"),
    ("E1", "task", "data_protocol_terms"),
])
def test_transfer_use_keeps_original_anchor_category(anchor_id, basis, field):
    profile = {**PROFILE, field: ["bootstrap confidence intervals"]}
    transfer = {**TRANSFER, "manuscript_facet_id": anchor_id, "basis": basis}
    original = deepcopy((transfer, profile))
    passed, evidence = _transfer_evidence({"transfer": transfer}, CANDIDATE, profile, MANUSCRIPT)
    assert passed
    assert evidence["profile_anchor"] == {
        "id": anchor_id, "field": field, "text": "bootstrap confidence intervals"}
    assert evidence["evidence"]["basis"] == basis
    assert (transfer, profile) == original


@pytest.mark.parametrize("patch,failed_check", [
    ({"manuscript_facet_id": "D1"}, "profile_anchor_valid"),
    ({"manuscript_facet_id": "M99"}, "profile_anchor_valid"),
    ({"basis": "generic_similarity"}, "supported_basis"),
    ({"manuscript_quote": "We use bootstrap confidence intervals to compare all benchmark scores."}, "verbatim_manuscript_evidence"),
    ({"candidate_quote": "Bootstrap and permutation tests compare language model scores."}, "verbatim_candidate_evidence"),
    ({"explanation": ""}, "connection_explained"),
    ({"limitation": ""}, "transfer_limits_explained"),
])
def test_category_separation_keeps_evidence_and_scope_requirements(patch, failed_check):
    passed, evidence = verify({**TRANSFER, **patch})
    assert not passed
    assert failed_check in evidence["failed_checks"]
