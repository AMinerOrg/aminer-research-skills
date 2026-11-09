"""Regression of the observed planner/schema ambiguity, without API calls."""
from related_work_audit.planning import build_related_work_queries

BODY = 'We study random seed variance during language model pretraining and compare multiple choice evaluation formats.'
PROFILE = {'domain_terms': ['language model evaluation'], 'task_terms': ['random seed variance']}

def payload():
    return {'retrieval_profile': PROFILE, 'coverage_targets': [
        {'id': 'W1', 'dimension': 'method', 'topic': 'random seed variance', 'source_quote': BODY,
         'domain_id': 'D1', 'priority': 'primary'},
        {'id': 'W2', 'dimension': 'evaluation', 'topic': 'multiple choice evaluation', 'source_quote': BODY,
         'domain_id': 'D1', 'priority': 'primary'}], 'queries': [
        {'query': 'seed variance pretraining', 'search_terms': ['seed variance pretraining'],
         'methods': ['Google Scholar', 'arXiv search'], 'query_shape': 'keyword_phrase',
         'role': 'method_mechanism', 'target_id': 'W1'},
        {'query': 'multiple choice format sensitivity', 'search_terms': ['multiple choice format sensitivity'],
         'methods': ['Semantic Scholar'], 'query_shape': 'keyword_phrase',
         'role': 'direct_baseline', 'target_id': 'W2'}]}

def run(value):
    return build_related_work_queries(BODY, review_as_of='2024-11-26', llm_json=lambda *a, **k: value)

def test_legacy_method_array_cannot_replace_actual_target_query():
    queries = run(payload())
    assert [str(q) for q in queries] == [
        'seed variance pretraining language model evaluation',
        'multiple choice format sensitivity language model evaluation']
    assert queries[1].role == 'benchmark_or_evaluation'
    assert queries[0].coverage_profile_diagnostics['ignored_legacy_methods_items'] == 2


def test_invalid_target_does_not_restore_platform_override_or_invent_direction():
    value = payload()
    for t in value['coverage_targets']: t['dimension'] = 'named_prior_work'
    queries = run(value)
    assert all(not q.coverage_targets for q in queries)
    assert not any('Scholar' in str(q) or 'arXiv' in str(q) for q in queries)
    assert queries[0].coverage_profile_diagnostics['rejected_targets'][0]['failed_checks'] == ['known_dimension']


def test_exact_title_is_preserved_even_with_a_spurious_methods_field():
    value = payload()
    title = 'A Careful Study of Language Model Evaluation Variance'
    value['queries'].append({'query': title, 'query_shape': 'exact_title',
        'role': 'named_prior_work', 'methods': ['Google Scholar']})
    queries = run(value)
    assert any(str(q) == title and q.query_shape == 'exact_title' for q in queries)


def test_old_target_free_method_list_still_splits_named_research_methods():
    queries = run({'queries': [{'query': 'MethodAlpha versus MethodBeta',
        'role': 'direct_baseline', 'methods': ['MethodAlpha', 'MethodBeta']}]})
    assert [str(q) for q in queries] == ['MethodAlpha', 'MethodBeta']


def test_failed_selector_call_is_not_a_semantic_rejection():
    from related_work_audit.planning import select_relevant_records
    row = {'title': 'Language Model Evaluation with Random Seeds',
           'summary': 'We study random seed variance in language model evaluation.',
           'published': '2023-01-01', 'source': 'AMiner Open Platform',
           'aminer_pub_id': '0123456789abcdef01234567', 'missing_status': 'uncited',
           'retrieval_role': 'method_mechanism'}
    class RateLimitError(Exception): pass
    def failed(*args, **kwargs): raise RateLimitError()
    selected, diagnostics = select_relevant_records([row], local_title='A New Evaluation Study',
        queries=run(payload()), review_as_of='2024-11-26', llm_json=failed, manuscript_context=BODY)
    assert selected == []
    assert diagnostics['selection_status'] == 'failed_RateLimitError'
    assert diagnostics['decision_counts'] == {'selection_unavailable': 1}
    assert diagnostics['selection_schema_errors'] == []
    assert diagnostics['eligible_medium_not_returned_count'] == 0
    assert diagnostics['evaluation_view_status'] == 'selection_unavailable'
