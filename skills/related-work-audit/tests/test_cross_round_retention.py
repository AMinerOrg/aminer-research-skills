"""Observable feedback-union behavior, including evidence and cap boundaries."""

from copy import deepcopy
import pytest

from related_work_audit.coverage import assess_related_work_coverage
from related_work_audit.retention import merge_verified_rounds
from related_work_audit.retrieval import retrieve_related_work
from test_recall_evidence import PROFILE, QUERY, candidate, choice, select, cross_candidate, transfer_choice
from test_candidate_recall import semantic_candidate, semantic_choice, semantic_select


REFERENCES = [{"candidate_title": "Optical Wave Propagation in Crystalline Materials", "id": "B1"}]


def merge(previous, current=None, **kwargs):
    args = dict(new_source_records=[], local_title="A New Recommendation Study",
                review_as_of="2024-11-26", manuscript_profile=PROFILE,
                local_references=REFERENCES, bibliography_ready=True)
    args.update(kwargs)
    return merge_verified_rounds(previous, current or [], **args)


def test_verified_first_selection_survives_omission_and_preserves_evidence():
    first, _ = semantic_select(semantic_candidate(), semantic_choice())
    frozen = deepcopy(first)
    kept, diag = merge(first, new_source_records=[candidate(title=first[0]["title"], summary="Short metadata.")])
    assert len(kept) == 1 and diag["added_by_retention_count"] == 1
    assert kept[0]["same_domain_evidence"] == first[0]["same_domain_evidence"]
    assert kept[0]["selection_reason"] == first[0]["selection_reason"]
    assert first == frozen
    coverage = assess_related_work_coverage(kept, {"status": "ready_for_resolution", "records": REFERENCES})
    assert coverage["records"][0]["cross_round_retention"]["added_by_retention"]
    assert coverage["summary"]["partition_conservation"]["conserved"]


@pytest.mark.parametrize("update", [
    {"published": "2025-01-01"}, {"published": "2024"},
    {"title": "An Entirely Different Article on Protein Folding"},
    {"final_output_eligible": False},
])
def test_new_source_facts_override_old_selection(update):
    first, _ = select([candidate()], [choice()])
    kept, diag = merge(first, new_source_records=[candidate(**update)])
    assert not kept
    assert diag["decision_counts"] == {"revalidation_excluded": 1}


def test_coarser_same_year_metadata_does_not_erase_precise_date():
    first, _ = select([candidate(published="2024-05-01")], [choice()])
    kept, _ = merge(first, new_source_records=[candidate(published="2024")])
    assert kept[0]["published"] == "2024-05-01"


@pytest.mark.parametrize("evidence", ["bibliography", "relation"])
def test_new_citation_evidence_removes_previously_selected_work(evidence):
    first, _ = select([candidate()], [choice()])
    args = {"local_references": [{"candidate_title": first[0]["title"]}]} if evidence == "bibliography" else {"cited_controls": [candidate()]}
    kept, diag = merge(first, **args)
    if evidence == "bibliography":
        assert not kept and diag["decisions"][0]["disposition"] == "revalidation_excluded"
    else:
        assert len(kept) == 1  # External relations alone cannot override the input ledger.


def test_unverified_rows_and_controls_cannot_be_promoted():
    first, _ = select([candidate()], [choice()])
    first[0]["missing_status"] = "already_cited"
    kept, diag = merge(first + [candidate(2)])
    assert not kept and len(diag["decisions"]) == 2


def test_transfer_stays_extended_and_tampered_source_quote_fails():
    first, _ = select([cross_candidate()], [transfer_choice()])
    kept, _ = merge(first)
    assert kept[0]["tier"] == "extended" and kept[0]["relation_scope"] == "cross_domain_transfer"
    first[0]["transfer_evidence"]["evidence"]["candidate_quote"] = "An invented passage not in the candidate source."
    kept, _ = merge(first)
    assert not kept


def test_verified_elided_evidence_survives_the_round_union():
    first_fragment = "Sparse key value memory enables selective storage"
    second_fragment = "The mechanism retains evidence from long histories"
    row = cross_candidate(summary=first_fragment + ". Intervening detail. " + second_fragment + ".")
    first, _ = select([row], [transfer_choice(candidate_quote=first_fragment + " ... " + second_fragment)])
    assert len(first) == 1
    kept, diag = merge(first)
    assert len(kept) == 1 and diag["added_by_retention_count"] == 1


def test_round_ids_do_not_deduplicate_different_publications():
    first, _ = select([candidate(1)], [choice()])
    second, _ = select([candidate(2)], [choice()])
    kept, _ = merge(first, second)
    assert len(kept) == 2  # Both selectors used R1.


def test_punctuation_variants_deduplicate_without_mixing_version_dates():
    first, _ = select([candidate(1)], [choice()])
    second, _ = select([candidate(2, title=first[0]["title"] + "!", published="2022-01-01")], [choice()])
    kept, diag = merge(first, second, new_source_records=[candidate(2, title=first[0]["title"], published="2025-01-01")])
    assert len(kept) == 1 and kept[0]["published"] == first[0]["published"]
    assert diag["decision_counts"]["revalidation_excluded"] == 1
    kept, diag = merge(first, second)
    assert len(kept) == 1 and diag["decision_counts"]["duplicate_selected_work"] == 1


def test_core_first_shared_cap_and_earlier_priority_within_tier():
    previous, _ = select([candidate(i) for i in range(1, 31)], [choice(i, relevance="medium") for i in range(1, 31)])
    current, _ = select([candidate(i) for i in range(31, 61)], [choice(i) for i in range(1, 31)])
    kept, diag = merge(previous, current)
    assert len(kept) == 30 and all(row["tier"] == "core" for row in kept)
    assert diag["decision_counts"]["output_cap"] == 30
    current, _ = select([candidate(i) for i in range(31, 61)], [choice(i, relevance="medium") for i in range(1, 31)])
    kept, _ = merge(previous, current)
    assert [row["aminer_pub_id"] for row in kept] == [row["aminer_pub_id"] for row in previous]


def test_real_retrieval_path_retains_outside_second_shortlist_without_extra_calls():
    calls = {"search": 0, "select": 0}
    traces = []
    def provider(*args, **kwargs):
        calls["search"] += 1
        return [candidate(calls["search"])], {"provider_errors": [], "api_calls": [], "cost": {}}
    def preselect(rows, **kwargs):
        return rows[-1:], {"status": "completed", "shortlist_limit": 60, "bank_count": len(rows)}
    def selector(rows, **kwargs):
        calls["select"] += 1
        return select(rows, [choice()])
    kept, diag = retrieve_related_work(
        [QUERY], local_title="A New Recommendation Study", review_as_of="2024-11-26",
        proxy=None, aminer_api_key="test", aminer_search_fn=provider,
        plan_feedback=lambda *a, **k: ([QUERY], {}), select_relevant=selector,
        preselect_candidates=preselect, local_references=REFERENCES,
        bibliography_ready=True, trace=traces.append,
    )
    assert calls == {"search": 2, "select": 2}
    assert len(kept) == diag["selected_record_count"] == diag["kept_record_count"] == 2
    assert diag["cross_round_retention"]["added_by_retention_count"] == 1
    assert len(diag["selection_rounds"]) == 2
    assert sum(t["event"] == "cross_round_retention" for t in traces) == 1


def test_second_selector_failure_preserves_first_evidence_and_reports_partial():
    calls = []
    def selector(rows, **kwargs):
        calls.append(len(rows))
        if len(calls) == 2:
            return [], {"selection_status": "failed_TimeoutError", "selected_record_count": 0}
        return select(rows, [choice()])
    kept, diag = retrieve_related_work(
        [QUERY], local_title="A New Recommendation Study", review_as_of="2024-11-26",
        proxy=None, aminer_api_key="test",
        aminer_search_fn=lambda *a, **k: ([candidate()], {"provider_errors": [], "cost": {}}),
        plan_feedback=lambda *a, **k: ([QUERY], {}), select_relevant=selector,
        local_references=REFERENCES, bibliography_ready=True,
    )
    assert len(kept) == diag["kept_record_count"] == 1
    assert diag["status"] == "partial_selection"
    assert diag["selection_rounds"][1]["selection_status"] == "failed_TimeoutError"
