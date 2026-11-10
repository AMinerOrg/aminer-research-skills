"""Observable recall boundaries using synthetic API responses; no paid calls."""
import json
from copy import deepcopy

import httpx
import pytest

from related_work_audit.aminer_search import (
    AMINER_PAPER_SEARCH_PRO_URL, AMINER_PAPER_SEARCH_URL, AMINER_PAPER_INFO_URL,
    _inspect_search_rows, retrieve_aminer_records,
)
from related_work_audit.planning import RoutedQuery, build_related_work_queries
from related_work_audit.rw_coverage import source_targets, scoped_phrase, QUERY_WORD_LIMIT, balanced_feedback
from related_work_audit.retrieval import filter_retrieval_records


def row(n, title, **fields):
    return {"id": f"{n:024x}", "title": title, "year": 2024, **fields}


def retrieve(response_rows, query="cross-domain machine text detection benchmark", shape="keyword_phrase"):
    calls = []
    def handler(request):
        calls.append(request)
        url = str(request.url).split("?", 1)[0]
        if url in {AMINER_PAPER_SEARCH_PRO_URL, AMINER_PAPER_SEARCH_URL}:
            return httpx.Response(200, json={"code": 200, "data": response_rows})
        if url == AMINER_PAPER_INFO_URL:
            return httpx.Response(200, json={"code": 200, "data": []})
        raise AssertionError(f"Unexpected API: {url}")
    records, diag = retrieve_aminer_records(
        [{"query": query, "query_shape": shape, "fallback_phrase": query}],
        review_as_of="2024-12-31", api_key="test-key", proxy=None,
        search_tier="pro", detail_limit=0, allow_keyword_expansion=False,
        client_factory=lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), **kw),
    )
    return records, diag, calls


def test_mixed_response_does_not_drop_synonym_title_when_one_title_matches():
    # The second paper describes the same task with different morphology.
    source = [row(1, "Machine Text Detection"),
              row(2, "A Shared Corpus for Evaluating Machine-Generated Text Detectors",
                  abstract="We benchmark human versus generated writing across domains.")]
    records, diag, calls = retrieve(source)
    assert {r["aminer_pub_id"] for r in records} == {r["id"] for r in source}
    synonym = next(r for r in records if r["aminer_pub_id"] == source[1]["id"])
    assert not synonym["query_title_overlap_hint"]
    assert synonym["summary"] == source[1]["abstract"]
    assert len(calls) == 2  # one search plus free info, no added search loop
    audit = diag["search_responses"][0]
    assert audit["provider_record_count"] == audit["retained_record_count"] == 2
    assert audit["records"][1]["disposition"] == "retained_for_semantic_selection"
    assert "tier" not in synonym and "selection_evidence" not in synonym


def test_all_provider_rows_are_explainable_without_serializing_unrelated_fields():
    source = [row(1, "Different Words", abstract="A complete supplied abstract.", Authorization="secret"),
              row(2, ""), {"id": "invalid", "title": "Missing verified identity"}]
    original = deepcopy(source)
    kept, audit = _inspect_search_rows(source, {
        "query": "retrieval grounded profiles", "query_shape": "keyword_phrase",
        "fallback_phrase": "retrieval grounded profiles",
    }, "pro")
    assert len(kept) == 1 and audit["provider_record_count"] == 3
    assert [r["disposition"] for r in audit["records"]] == [
        "retained_for_semantic_selection", "invalid_identity", "invalid_identity"]
    assert "secret" not in json.dumps(audit)
    assert source == original


def test_exact_full_title_identity_remains_strict_and_rejections_are_logged():
    title = "A Complete Title for One Specific Study"
    records, diag, _ = retrieve([row(1, title), row(2, title + " Revisited")], title, "exact_title")
    assert [r["title"] for r in records] == [title]
    assert diag["search_responses"][0]["records"][1]["disposition"] == "exact_title_mismatch"


def test_retaining_title_mismatches_does_not_bypass_year_self_or_citation_controls():
    source = [row(1, "Shared Corpus for Text Detectors"),
              row(2, "A Future Corpus", year=2025), row(3, "The Manuscript Itself"),
              row(4, "An Already Cited Corpus")]
    records, _, _ = retrieve(source)
    kept, diag = filter_retrieval_records(records, local_title="The Manuscript Itself",
        review_as_of="2024-12-31", manuscript_profile={"domain_terms": ["text detectors"]},
        bibliography_ready=True, local_references=[{
            "candidate_title": source[3]["title"], "aminer_pub_id": source[3]["id"],
            "resolution_eligible": True,
        }])
    assert source[1]["title"] not in {r["title"] for r in kept}
    assert source[2]["title"] not in {r["title"] for r in kept}
    cited = next(r for r in kept if r["title"] == source[3]["title"])
    assert cited["local_citation_match"]["coverage_status"] == "already_cited"
    assert any(r["title"] == source[0]["title"] and r["published"] == "2024" for r in kept)
    assert diag["future_records_removed"] == diag["self_records_removed"] == 1


BODY = "We study underwater robot navigation. We measure success in previously unseen environments using several benchmark datasets."
PROFILE = {"domain_terms": ["underwater robot navigation", "image depth reconstruction"]}


def target(n, **patch):
    return {"id": f"W{n}", "dimension": "method", "topic": "navigation approaches",
            "source_quote": BODY, "domain_id": "D1", "priority": "primary", **patch}


def test_invalid_quote_cannot_make_queries_inherit_a_different_targets_domain():
    payload = {"retrieval_profile": PROFILE,
        "coverage_targets": [target(1, domain_id="D2"),
                             target(2, dimension="dataset", source_quote="This is not in the manuscript.")],
        "queries": [{"query": "benchmark datasets across environments", "target_id": "W2",
                     "query_shape": "keyword_phrase", "role": "benchmark_or_evaluation", "facet": "dataset"}]}
    plans = build_related_work_queries(BODY, review_as_of="2024-12-31", llm_json=lambda *a, **kw: payload)
    data_query = next(p for p in plans if p.target_id == "W2")
    assert "underwater robot navigation" in data_query
    assert "image depth reconstruction" not in data_query
    diag = data_query.coverage_profile_diagnostics
    assert {t["id"] for t in data_query.coverage_targets} == {"W1"}
    assert diag["query_target_contexts"]["W2"]["status"] == "query_only_unvalidated_target"
    assert diag["rejected_targets"][0]["failed_checks"] == ["verbatim_source_quote"]


def test_missing_domain_is_explicit_instead_of_using_first_valid_domain():
    payload = {"retrieval_profile": PROFILE,
        "coverage_targets": [target(1), target(2, domain_id="D99")],
        "queries": [{"query": "evaluation benchmark coverage", "target_id": "W2",
                     "query_shape": "keyword_phrase", "facet": "evaluation"}]}
    plans = build_related_work_queries(BODY, review_as_of="2024-12-31", llm_json=lambda *a, **kw: payload)
    p = next(p for p in plans if p.target_id == "W2")
    assert "underwater" not in p and "image" not in p
    repair = next(x for x in p.coverage_profile_diagnostics["query_transformations"] if x["target_id"] == "W2")
    assert repair["domain_status"] == "unresolved_keep_query_context"


def test_unique_explicit_facet_and_domain_repair_still_requires_a_real_quote():
    payload = {"retrieval_profile": PROFILE,
        "coverage_targets": [target(1, dimension="named_prior_work", domain_id="D99")],
        "queries": [{"target_id": "W1", "facet": "dataset",
                     "query": "underwater robot navigation benchmark"}]}
    accepted, diag = source_targets(payload, BODY)
    assert accepted[0]["dimension"] == "dataset" and accepted[0]["domain_id"] == "D1"
    assert len(diag["target_repairs"]) == 2
    payload["coverage_targets"][0]["source_quote"] = "A sentence absent from the input manuscript."
    assert source_targets(payload, BODY)[0] == []


@pytest.mark.parametrize("phrase,domain,dimension,cue", [
    ("diverse prompt templates across models text dataset cross-domain evaluation", "generated text detection", "dataset", "dataset"),
    ("multiple tasks generalization across datasets measured under distribution shifts robustness", "robot navigation", "evaluation", "robustness"),
    ("long context language model language model output probability calibration", "language model probability calibration", "method", "context"),
])
def test_compact_query_retains_domain_and_direction_without_duplicate_conditions(phrase, domain, dimension, cue):
    actual = scoped_phrase(phrase, domain, dimension)
    tokens = actual.casefold().split()
    assert len(tokens) <= QUERY_WORD_LIMIT
    assert len(tokens) == len(set(tokens))
    assert set(domain.casefold().split()) <= set(tokens) and cue in tokens


@pytest.mark.parametrize("phrase,domain", [
    ("large language models random number generation randomness", "LLM token probabilities numeric context"),
    ("earth mover distance calibration metric predictive distributions", "language model output probability calibration"),
])
def test_shortening_does_not_cut_a_multiword_search_concept(phrase, domain):
    actual = scoped_phrase(phrase, domain)
    assert actual and phrase in actual


def test_duplicate_feedback_uses_an_available_later_proposal_within_budget():
    accepted, _ = source_targets({"retrieval_profile": PROFILE,
        "coverage_targets": [target(1, topic="navigation approaches")]}, BODY)
    # Original query includes the full grounded fallback, making it a duplicate.
    initial = scoped_phrase("navigation approaches", PROFILE["domain_terms"][0])
    q = RoutedQuery(initial, query_shape="keyword_phrase", fallback_phrase=initial,
        role="problem_setting", pro_field="keyword", retrieval_profile=PROFILE, coverage_targets=accepted)
    plans, diag = balanced_feedback(original_queries=[q], selected=[], controls=[],
        coverage={"targets": [{**accepted[0], "status": "uncovered"}]}, review_as_of="2024-12-31",
        query_type=RoutedQuery, llm_json=lambda *a, **kw: {"queries": [
            {"target_id": "W1", "search_terms": "navigation approaches"},
            {"target_id": "W1", "search_terms": "unfamiliar environment localization"},
        ]})
    assert len(plans) == 1 and "unfamiliar environment localization" in plans[0]
    assert diag["target_dispositions"][0]["repair_basis"] == "later_model_alternative"
    assert diag["feedback_query_budget"]["used_queries"] == 1


def test_complete_titles_and_acronyms_are_not_truncated_with_discovery_queries():
    exact = "A Long and Complete Title on Underwater Robot Navigation and Benchmark Design"
    payload = {"retrieval_profile": PROFILE, "coverage_targets": [target(1)],
        "exact_titles": [exact, "NAVBench"], "queries": []}
    plans = build_related_work_queries(BODY, review_as_of="2024-12-31", llm_json=lambda *a, **kw: payload)
    assert exact in plans and "NAVBench" in plans
    assert len(plans) <= 10
