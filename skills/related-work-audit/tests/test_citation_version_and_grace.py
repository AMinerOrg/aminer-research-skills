"""External graph versions cannot replace the current PDF's bibliography."""
from copy import deepcopy
import pytest

from related_work_audit.coverage import assess_related_work_coverage
from related_work_audit.publication_time import review_year_end
from related_work_audit.retrieval import filter_retrieval_records, retrieve_related_work, partition_relation_references
from related_work_audit.reference_neighbors import expand_reference_neighbors
from related_work_audit.retention import merge_verified_rounds
from related_work_audit.aminer_search import _search_once_request
from test_recall_evidence import candidate, choice, select, PROFILE, QUERY

REFS = [{"id": "B1", "candidate_title": "Optical Wave Propagation in Crystalline Materials"}]

def graph_record(**changes):
    row = candidate(relation_role="target_references", retrieval_role="citation_graph_reference")
    row.update(changes)
    return row

@pytest.mark.parametrize('ready', [False, True])
def test_external_reference_does_not_establish_current_pdf_citation(ready):
    record = graph_record()
    frozen = deepcopy(record)
    hints, controls = partition_relation_references([record], local_references=REFS, bibliography_ready=ready)
    assert len(hints) == 1 and not controls
    assert hints[0]['missing_status'] == ('uncited' if ready else 'unknown')
    report = assess_related_work_coverage([record], {'status': 'ready_for_resolution' if ready else 'unready', 'records': REFS}, relation_references=[record])
    assert report['records'][0]['status'] == ('candidate_to_check' if ready else 'not_assessed')
    assert report['records'][0]['match'] is None
    assert report['records'][0]['external_reference_hint']
    assert record == frozen

def test_real_local_citation_still_excluded_and_can_seed_search():
    record = graph_record()
    refs = [{'id':'B2', 'candidate_title':record['title']}]
    hints, controls = partition_relation_references([record], local_references=refs, bibliography_ready=True)
    assert not hints and len(controls) == 1
    assert controls[0]['bibliography_match']['evidence_source'] == 'parsed_local_bibliography'
    report = assess_related_work_coverage([record], {'status':'ready_for_resolution','records':refs}, relation_references=[record])
    assert report['records'][0]['status'] == 'covered'
    assert report['records'][0]['match']['reference_id'] == 'B2'

def test_graph_hint_reaches_normal_candidate_filter_and_selector():
    captured = []
    def selector(rows, **kwargs):
        captured.extend(rows)
        return [], {'status':'completed'}
    _, diag = retrieve_related_work([QUERY], local_title='A New Recommendation Study',
        review_as_of='2024-12-31', proxy=None, select_relevant=selector,
        local_references=REFS, bibliography_ready=True, known_cited_controls=[graph_record()],
        aminer_api_key='mock', aminer_search_fn=lambda *a, **kw: ([], {'cost':{'calls':0}}))
    assert len(captured) == 1
    assert captured[0]['missing_status'] == 'uncited'
    assert captured[0]['relation_role'] == 'target_reference_hint'
    assert diag['already_cited_control_count'] == 0

def test_unconfirmed_graph_edge_never_becomes_a_manuscript_cited_seed():
    def forbidden(*args, **kwargs):
        pytest.fail('No local cited seed exists')
    neighbors, diag = expand_reference_neighbors([], known_cited_controls=[graph_record()],
        bibliography_ready=False, queries=[QUERY], local_title='A New Recommendation Study',
        review_as_of='2024-12-31', api_key='mock', proxy=None, resolve=forbidden, fetch_relations=forbidden)
    assert not neighbors and diag['eligible_seed_count'] == 0

def test_external_reference_update_does_not_erase_verified_selection():
    first, _ = select([candidate()], [choice()])
    assert first
    kept, _ = merge_verified_rounds(first, [], new_source_records=[graph_record()],
        local_title='A New Recommendation Study', review_as_of='2024-12-31',
        manuscript_profile=PROFILE, local_references=REFS, bibliography_ready=True,
        cited_controls=[graph_record()])
    assert len(kept) == 1

def test_one_year_allowance_is_consistent_at_api_and_local_filter():
    assert review_year_end('2024', 1) == '2025-12-31'
    assert review_year_end('2024-10-01', 1) == '2025-12-31'
    cutoff = review_year_end('2024', 1)
    plan = {'query':'Example Complete Paper Title', 'fallback_phrase':'example paper', 'query_shape':'exact_title'}
    for tier in ('free', 'pro', 'qa-pro'):
        assert _search_once_request(plan, tier, int(cutoff[:4]))[3]['year_to'] == 2025
    rows, diag = filter_retrieval_records([candidate(published='2025'), candidate(2, published='2026')],
        local_title='New study', review_as_of=cutoff, local_references=REFS, bibliography_ready=True)
    assert len(rows) == 1 and rows[0]['published'] == '2025'
    assert diag['future_records_removed'] == 1
    strict, _ = filter_retrieval_records([candidate(published='2025')], local_title='New study', review_as_of=review_year_end('2024'))
    assert strict == []

@pytest.mark.parametrize('grace', [-1, 2, True, '1'])
def test_invalid_year_allowance_is_rejected(grace):
    with pytest.raises(ValueError):
        review_year_end('2024', grace)
