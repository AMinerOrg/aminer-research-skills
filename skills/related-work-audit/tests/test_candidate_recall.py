"""Conserve discovery routes and usable evidence before semantic selection."""

import pytest

from related_work_audit.aminer_search import _publication_evidence, _choose_publication_evidence
from related_work_audit.planning import _routed_queries_from_payload, select_relevant_records
from related_work_audit.publication_time import arxiv_month_evidence
from related_work_audit.retrieval import coherent_domain_score, filter_retrieval_records, retrieve_related_work
from related_work_audit.candidate_pool import semantic_candidate_pool
from related_work_audit.references import build_reference_ledger
from test_recall_evidence import PROFILE, QUERY, candidate, choice


def test_named_titles_cannot_displace_discovery_facets_or_search_self():
    title = "A New Study of Sequential Recommendation with User Memory"
    payload = {"exact_titles": [title] + [f"Known Prior Work Number {i}" for i in range(8)],
               "queries": [{"query": f"distinct mechanism {i}", "role": "method_mechanism"} for i in range(4)] +
               [{"query": "user behavior prediction", "role": "problem_setting"},
                {"query": "long history evaluation", "role": "benchmark_or_evaluation"},
                {"query": "alternative sequence formulation", "role": "alternative_formulation"}]}
    plans = _routed_queries_from_payload(payload, limit=10, local_title=title)
    assert len(plans) <= 10
    assert title not in plans
    assert all(q["query"] in plans for q in payload["queries"])


def test_broad_query_cannot_crowd_out_independent_candidates():
    rows = [candidate(i, title=f"Sequential Recommendation with Sparse Key Value Memory {i}", query="broad query") for i in range(90)]
    rows += [candidate(100+i, title=f"Alternative Work {i}", query=f"independent query {i}") for i in range(5)]
    kept, diag = filter_retrieval_records(rows, local_title="Sequential Recommendation with Sparse Key Value Memory", review_as_of="2024-11-26", manuscript_profile=PROFILE)
    assert all(f"independent query {i}" in {row["query"] for row in kept} for i in range(5))
    assert len(kept) <= 80
    assert sum(d["disposition"] == "kept" for d in diag["filter_decisions"]) == len(kept)


def test_cited_controls_do_not_consume_discovery_slots():
    rows = [candidate(i) for i in range(1, 84)]
    rows[0]["title"] = "Optical Wave Propagation in Crystalline Materials"
    refs = [{"candidate_title": rows[0]["title"], "aminer_pub_id": rows[0]["aminer_pub_id"], "resolution_eligible": True}]
    kept, diag = filter_retrieval_records(rows, local_title="An Unrelated Manuscript Title", review_as_of="2024-11-26", max_records=5, local_references=refs, bibliography_ready=True)
    assert len([r for r in kept if not r.get("local_citation_match")]) == 5
    assert diag["preselection_controls_outside_budget"] >= 1


def test_domain_ranking_retains_qualifiers_without_excluding_cross_domain():
    profile = {"domain_terms": ["long-context language models", "LLM agent design"]}
    relevant = {"title": "Efficient Long Context Language Models", "summary": "A language model with a larger context."}
    irrelevant = {"title": "Long Short Term Memory for Mechanical Design", "summary": "We model a long sequence and consider the context of mechanical agents."}
    assert coherent_domain_score(relevant, profile) > 0
    assert coherent_domain_score(irrelevant, profile) == 0


@pytest.mark.parametrize("doi,expected", [
    ("10.48550/arXiv.2406.12345", "2024-06"),
    ("https://doi.org/10.48550/arxiv.2401.12345v2", "2024-01"),
    ("10.48550/arxiv.2413.12345", None),
    ("10.1234/2406.12345", None),
    ("a paper mentions 2406.12345", None),
])
def test_only_explicit_valid_arxiv_identifiers_supply_month(doi, expected):
    evidence = arxiv_month_evidence({"doi": doi}, "test")
    assert (evidence[0]["value"] if evidence else None) == expected


def test_conflicting_arxiv_identifiers_do_not_supply_date():
    assert arxiv_month_evidence({"doi": "10.48550/arxiv.2406.12345", "arxiv_id": "2412.23456"}, "test") == []


@pytest.mark.parametrize("identifier,expected", [("2406.12345", 1), ("2411.12345", 0), ("2412.12345", 0)])
def test_month_bound_preserves_intra_month_cutoff_and_final_selection(identifier, expected):
    row = candidate(published="2024", doi="10.48550/arxiv."+identifier)
    kept, diag = filter_retrieval_records([row], local_title="An Unrelated Manuscript Title", review_as_of="2024-11-26", manuscript_profile=PROFILE)
    selected, _ = select_relevant_records(kept, local_title="An Unrelated Manuscript Title", queries=[QUERY], review_as_of="2024-11-26", llm_json=lambda *a, **k: {"selected": [choice()]})
    assert len(selected) == expected
    if selected:
        assert selected[0]["published"] == "2024-06"
        assert selected[0]["date_precision"] == "month"
        assert any(e.get("basis") == "arxiv_original_submission_month" for e in selected[0]["publication_date_evidence"])


def test_preprint_month_survives_later_venue_year_without_invented_day():
    evidence = _publication_evidence({"doi": "10.48550/arxiv.2406.12345", "year": 2025}, "paper_info")
    assert _choose_publication_evidence(evidence) == "2024-06"
    evidence = _publication_evidence({"publication_date": "2024-09", "year": 2024}, "paper_detail")
    assert _choose_publication_evidence(evidence) == "2024-09"


CONTEXT = "We study sequential recommendation by predicting the next item from users' interaction histories."
SYNONYM_ABSTRACT = "Next-item ranking predicts users' choices using their past interactions."


def semantic_choice(**overrides):
    return choice(relation_scope="same_domain", domain_evidence={
        "primary_domain": "sequential recommendation",
        "manuscript_quote": "predicting the next item from users' interaction histories",
        "candidate_quote": SYNONYM_ABSTRACT,
        "explanation": "Both address the next choice from a user's interaction history, using different terminology.",
        **overrides,
    })


def semantic_candidate(**overrides):
    row = candidate(title="Next-Item Ranking from Interaction Histories", summary=SYNONYM_ABSTRACT,
                    facet_alignment={"profile_available": True, "task_domain_fit": False,
                                     "publication_fit": False, "mechanism_only_cross_domain": True})
    row.update(overrides)
    return row


def semantic_select(row, selected):
    return select_relevant_records([row], local_title="A New Recommendation Study", queries=[QUERY],
                                   review_as_of="2024-11-26", manuscript_context=CONTEXT,
                                   llm_json=lambda *a, **k: {"selected": [selected]})


def test_verbatim_semantic_evidence_rescues_terminology_mismatch_as_extended():
    selected, diag = semantic_select(semantic_candidate(), semantic_choice())
    assert len(selected) == 1
    assert selected[0]["tier"] == "extended"
    assert selected[0]["relation_scope"] == "same_domain"
    assert selected[0]["same_domain_evidence"]["passed"]
    assert diag["decision_counts"] == {"selected": 1}


@pytest.mark.parametrize("invalid", [
    {"candidate_quote": "A passage absent from the supplied candidate."},
    {"manuscript_quote": "A passage absent from the supplied manuscript."},
    {"primary_domain": "a domain not present in the profile"},
])
def test_ungrounded_same_domain_claim_cannot_bypass_lexical_gate(invalid):
    selected, _ = semantic_select(semantic_candidate(), semantic_choice(**invalid))
    assert selected == []


def test_missing_relationship_in_both_equivalent_fields_is_rejected():
    item = semantic_choice(explanation="")
    item["reason"] = ""
    selected, _ = semantic_select(semantic_candidate(), item)
    assert selected == []


@pytest.mark.parametrize("overrides", [
    {"published": "2024"}, {"published": "2025-01-01"},
    {"missing_status": "already_cited"}, {"local_citation_match": {"coverage_status": "already_cited"}},
    {"title": "A New Recommendation Study"},
])
def test_semantic_evidence_never_bypasses_date_self_or_citation(overrides):
    selected, _ = semantic_select(semantic_candidate(**overrides), semantic_choice())
    assert selected == []


def pool(rows, model):
    return semantic_candidate_pool(rows, local_title="A New Manuscript", review_as_of="2024-11-26",
        manuscript_profile=PROFILE, manuscript_context=CONTEXT, llm_json=model)


def test_semantic_bank_is_bounded_and_unresolved_dates_do_not_consume_it():
    rows = [candidate(i, retrieval_record_index=i) for i in range(510)]
    rows.insert(0,candidate(999,published="2024",time_admissibility="uncertain",retrieval_record_index=999))
    captured = {}
    def model(system,prompt,**kwargs):
        captured['prompt'] = prompt
        return {'ranked':[{'id':'P500'},{'id':'P501'},{'id':'P500'}]}
    chosen,diag = pool(rows,model)
    assert len(chosen) == 1 and chosen[0]['retrieval_record_index'] == 499
    assert diag['bank_count'] == 500 and diag['bank_cap_omitted'] == 10
    assert diag['pending_record_indices'] == [999]
    assert diag['status'] == 'partial_invalid_ids'
    assert 'P501' not in captured['prompt']


def test_small_bank_skips_extra_model_call_and_failure_is_explicit():
    def fail(*a,**k):
        raise RuntimeError('model unavailable')
    chosen,diag = pool([candidate(1)],fail)
    assert len(chosen) == 1 and diag['status'] == 'not_needed'
    chosen,diag = pool([candidate(i) for i in range(80)],fail)
    assert len(chosen) == 60 and diag['status'] == 'failed_fallback'
    assert diag['error_type'] == 'RuntimeError'


def test_late_candidate_reaches_selector_before_old_eighty_record_cutoff():
    rows = [candidate(i) for i in range(1,101)]
    def provider(*a,**k):
        return rows, {'provider_errors':[], 'api_calls':[], 'cost':{}}
    def preselect(records,**kwargs):
        return semantic_candidate_pool(records,manuscript_context=CONTEXT,
            llm_json=lambda *a,**k:{'ranked':[{'id':'P100'}]},**kwargs)
    chosen,diag = retrieve_related_work([QUERY],local_title="An Unrelated Manuscript",review_as_of="2024-11-26",
        proxy=None,aminer_api_key='test-key',aminer_search_fn=provider,preselect_candidates=preselect,
        select_relevant=lambda records,**kwargs:(records,{'selection_status':'completed'}))
    assert len(chosen) == 1 and chosen[0]['aminer_pub_id'] == f'{100:024x}'
    decisions = diag['filter_decisions']
    assert sum(d['disposition']=='kept' for d in decisions) == 1
    assert sum(d['disposition']=='semantic_preselection_not_returned' for d in decisions) == 99
    assert len(decisions) == 100 and diag['status'] == 'passed'


def test_semantic_preselection_failure_propagates_partial_status():
    rows = [candidate(i) for i in range(80)]
    def provider(*a,**k):
        return rows, {'provider_errors':[], 'api_calls':[], 'cost':{}}
    def preselect(records,**kwargs):
        return semantic_candidate_pool(records,manuscript_context=CONTEXT,
            llm_json=lambda *a,**k:{},**kwargs)
    selected,diag = retrieve_related_work([QUERY],local_title="An Unrelated Manuscript",review_as_of="2024-11-26",
        proxy=None,aminer_api_key='test-key',aminer_search_fn=provider,preselect_candidates=preselect,
        select_relevant=lambda records,**kwargs:(records[:1],{'selection_status':'completed'}))
    assert selected and diag['status']=='partial_selection'
    assert diag['candidate_pool_round_statuses'] == ['failed_fallback']


def test_author_initials_do_not_end_primary_bibliography():
    pages = [
        'References\nAlice Author. First Complete Reference Title. Journal, 2021.\n'
        'Bernardino Researcher, Mohammadamin Researcher,\n'
        'M Pawan Kumar, Emilien Dupont, Francisco JR Ruiz, Jordan S Ellenberg, Pengming Wang,\n'
        'Omar Fawzi, et al. A Study of Program Search. Nature, 2024.',
        'Nina Author. A Later Reference That Must Remain Visible. Journal, 2023.\n'
        'Zelda Author. The Last Main Reference Title. Journal, 2024.',
        'A Evaluation Details\nThis appendix describes the experiment.\n'
        'References\nOther Author. A Reference From an Embedded Artifact. Journal, 2020.',
    ]
    ledger = build_reference_ledger(pages)
    raw = ' '.join(r['raw'] for r in ledger['records'])
    assert 'Later Reference That Must Remain Visible' in raw
    assert 'Last Main Reference Title' in raw
    assert 'Embedded Artifact' not in raw


def test_semantic_pool_conserves_compact_cited_control_records():
    rows = [candidate(i) for i in range(1,101)]
    rows[0]['title'] = 'Optical Wave Propagation in Crystalline Materials'
    refs = [{'candidate_title':rows[0]['title'], 'resolution_eligible':True}]
    def provider(*a,**k):
        return rows, {'provider_errors':[], 'api_calls':[], 'cost':{}}
    def preselect(records,**kwargs):
        return semantic_candidate_pool(records,manuscript_context=CONTEXT,
            llm_json=lambda *a,**k:{'ranked':[{'id':'P99'}]},**kwargs)
    selected,diag = retrieve_related_work([QUERY],local_title="An Unrelated Manuscript",review_as_of="2024-11-26",
        proxy=None,aminer_api_key='test-key',aminer_search_fn=provider,preselect_candidates=preselect,
        local_references=refs,bibliography_ready=True,
        select_relevant=lambda records,**kwargs:(records,{'selection_status':'completed'}))
    assert len(selected)==1 and selected[0]['aminer_pub_id']==f'{100:024x}'
    assert diag['already_cited_control_count']==1
    assert sum(d['disposition']=='known_cited_control' for d in diag['filter_decisions'])==1
