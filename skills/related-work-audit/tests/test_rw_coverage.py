"""Behavioral checks for grounded coverage and bounded query allocation."""
from copy import deepcopy

from related_work_audit.planning import RoutedQuery, build_related_work_queries, build_feedback_queries
from related_work_audit.rw_coverage import (
    source_targets, related_work_coverage, attach_initial_targets, balanced_feedback,
)

BODY = "We evaluate robot navigation with uncertainty estimates and compare datasets collected in unfamiliar indoor environments."
PROFILE = {"domain_terms": ["indoor robot navigation"], "task_terms": ["unfamiliar environment navigation"],
           "mechanism_terms": ["bootstrap uncertainty estimates"], "data_protocol_terms": ["robot navigation datasets"]}


def target(n=1, dimension="task", **kwargs):
    return {"id": f"W{n}", "dimension": dimension, "topic": f"unfamiliar environments {n}",
            "source_quote": BODY, "domain_id": "D1", "priority": "primary", **kwargs}


def prepared(raw):
    return source_targets({"retrieval_profile": PROFILE, "coverage_targets": raw}, BODY)[0]


def query(text="prior navigation findings", **kwargs):
    return RoutedQuery(text, query_shape="keyword_phrase", fallback_phrase=text,
                       role="problem_setting", pro_field="keyword", retrieval_profile=PROFILE, **kwargs)


def test_targets_require_source_and_valid_primary_domain_and_preserve_inputs():
    payload = {"retrieval_profile": PROFILE, "coverage_targets": [target(),
        target(2, source_quote="This sentence does not occur anywhere in the supplied manuscript."),
        target(3, domain_id="M1"), target(4, dimension="invented")]}
    original = deepcopy(payload)
    targets, diagnostics = source_targets(payload, BODY)
    assert [x["id"] for x in targets] == ["W1"]
    assert targets[0]["domain_context"] == "indoor robot navigation"
    assert len(diagnostics["rejected_targets"]) == 3
    assert payload == original


def test_initial_domain_context_preserves_exact_titles_and_query_budget():
    raw = [target(i+1, dimension) for i, dimension in enumerate(["method"]*7+["task","dataset","evaluation"])]
    targets = prepared(raw)
    plans = [query(f"different target {i}", target_id=t["id"]) for i,t in enumerate(targets)]
    exact = RoutedQuery("A complete, distinctive title of a navigation paper", query_shape="exact_title",
        fallback_phrase="A complete distinctive title", role="named_prior_work", pro_field="title", retrieval_profile=PROFILE)
    scoped = attach_initial_targets([exact]+plans, targets, {"status":"ready"}, RoutedQuery)
    assert len(scoped) == 10
    assert str(exact) in [str(x) for x in scoped]
    discovery = [x for x in scoped if x.role != "named_prior_work"]
    assert {x.facet for x in discovery[:4]} == {"method","task","dataset","evaluation"}
    assert all("indoor robot navigation" in x.fallback_phrase for x in discovery)
    assert all(x.pro_field == "keyword" for x in discovery)


def test_real_initial_planner_contract_attaches_validated_targets():
    payload = {"retrieval_profile": PROFILE, "coverage_targets": [target(topic="uncertainty estimates")],
        "queries": [{"query":"uncertainty estimates", "search_terms":["uncertainty", "estimates"],
                     "role":"problem_setting", "target_id":"W1", "query_shape":"keyword_phrase"}]}
    queries=build_related_work_queries(BODY, review_as_of="2024-11-26", llm_json=lambda *a,**k:payload)
    assert queries[0].coverage_targets[0]["source_quote"] == BODY
    assert str(queries[0]) == "uncertainty estimates indoor robot navigation"


def test_missing_queries_preserve_full_profile_and_original_domain_ordinals():
    profile = {**PROFILE, "domain_terms": ["", "indoor robot navigation", "indoor robot navigation", "outdoor robot navigation"]}
    payload = {"retrieval_profile": profile, "coverage_targets": [
        target(topic="uncertainty estimates", domain_id="D4")], "queries": []}
    queries = build_related_work_queries(BODY, review_as_of="2024-11-26", llm_json=lambda *a, **k: payload)
    assert len(queries) == 1
    assert str(queries[0]) == "uncertainty estimates outdoor robot navigation"
    assert queries[0].retrieval_profile == {**PROFILE, "domain_terms": ["indoor robot navigation", "outdoor robot navigation"]}
    assert queries[0].coverage_targets[0]["source_domain_id"] == "D4"
    assert queries[0].coverage_targets[0]["domain_id"] == "D2"


def test_role_labels_query_matches_and_unverified_or_cited_papers_are_not_coverage():
    targets=prepared([target(topic="bootstrap uncertainty estimates")])
    queries=[query(coverage_targets=targets)]
    candidate={"title":"Bootstrap uncertainty estimates for robots", "summary":"Concrete study.",
        "tier":"extended", "missing_status":"uncited", "relation_scope":"same_domain",
        "retrieval_role":"method_mechanism", "verification":{"relevance":{"passed":True}}}
    assert related_work_coverage([candidate],queries)["sufficient"]
    for patch in ({"title":"Unrelated work", "query":str(queries[0])},
                  {"missing_status":"already_cited"},{"missing_status":"unknown"},
                  {"relation_scope":"cross_domain_transfer"},{"verification":{}}):
        assert not related_work_coverage([{**candidate,**patch}],queries)["sufficient"]


def test_feedback_gives_supported_dimensions_slots_under_same_eight_query_budget():
    targets=prepared([target(i+1,d) for i,d in enumerate(["method"]*7+["task","dataset","evaluation","alternative","finding"])])
    queries=[query(coverage_targets=targets)]
    coverage=related_work_coverage([],queries)
    plans,diag=balanced_feedback(original_queries=queries,selected=[],coverage=coverage,controls=[],
        review_as_of="2024-11-26",llm_json=lambda *a,**k:{"queries":[]},query_type=RoutedQuery)
    assert len(plans)==8
    assert {x.facet for x in plans[:6]}=={"method","task","dataset","evaluation","alternative","finding"}
    assert len({str(x) for x in plans})==8
    assert all("indoor robot navigation" in str(x) for x in plans)
    assert diag["target_conservation"]=={"total":12,"queried":8,"deferred_budget":4,"conserved":True}
    assert diag["feedback_query_budget"]["maximum_queries"]==8


def test_duplicate_queries_and_cited_titles_do_not_consume_budget_or_become_omissions():
    targets=prepared([target(1,topic="uncertainty estimates"),target(2,"evaluation",topic="success rate estimates")])
    queries=[query("uncertainty estimates indoor robot navigation",coverage_targets=targets)]
    coverage=related_work_coverage([],queries)
    plans,diag=balanced_feedback(original_queries=queries,selected=[],coverage=coverage,
        controls=[{"title":"A Prior Navigation Evaluation Study"}],review_as_of="2024-11-26",
        llm_json=lambda *a,**k:{"queries":[{"target_id":"W2","search_terms":"A Prior Navigation Evaluation Study"}]},query_type=RoutedQuery)
    assert plans==[]
    assert {x["status"] for x in diag["target_dispositions"]}=={"duplicate_query","excluded_cited_title"}
    assert diag["status"]=="no_new_query"


def test_model_failure_falls_back_to_grounded_targets_only():
    targets=prepared([target(topic="uncertainty estimates")])
    queries=[query(coverage_targets=targets)]
    coverage=related_work_coverage([],queries)
    def failed(*a,**k): raise TimeoutError("fixture")
    plans,diag=build_feedback_queries([],[],original_queries=queries,review_as_of="2024-11-26",
        llm_json=failed,technical_coverage={"related_work":coverage})
    assert len(plans)==1 and plans[0].target_id=="W1"
    assert diag["model_planner_status"]=="TimeoutError"
    assert plans[0].feedback_origin=="deterministic_target_completion"
