import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import pytest

from related_work_audit.audit_trace import AuditTrace
from related_work_audit.planning import select_relevant_records
from related_work_audit.reference_neighbors import expand_reference_neighbors
from related_work_audit.references import build_reference_ledger_from_grobid
from related_work_audit.retrieval import retrieve_related_work
from test_recall_evidence import PROFILE, QUERY, candidate, choice


def options():
    return dict(local_title='A New Recommendation Study', queries=[QUERY], review_as_of='2024-11-26',
                manuscript_context='Sequential recommendation with sparse key value memory.', batch_size=20)


def supplied(prompt):
    return json.loads(prompt.split('Candidate records:\n', 1)[1])


def select_all(_system, prompt, **kwargs):
    return {'selected': [choice(int(row['candidate_id'][1:])) for row in supplied(prompt)]}


def test_parallel_batches_overlap_and_match_serial_merge():
    rows = [candidate(i) for i in range(1, 61)]
    serial, serial_diag = select_relevant_records(rows, **options(), max_workers=1, llm_json=select_all)
    barrier = Barrier(3)
    lock = Lock()
    active = 0
    maximum = 0

    def model(*args, **kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        barrier.wait(timeout=5)
        result = select_all(*args, **kwargs)
        with lock:
            active -= 1
        return result

    parallel, diag = select_relevant_records(rows, **options(), max_workers=3, llm_json=model)
    assert maximum == 3
    assert parallel == serial
    assert len(parallel) == 30
    assert {row['selection_batch'] for row in parallel} == {1, 2, 3}
    assert diag['candidate_decisions'] == serial_diag['candidate_decisions']
    assert len({row['candidate_id'] for row in diag['candidate_decisions']}) == 60
    assert diag['selector_batching']['batch_sizes'] == [20, 20, 20]
    assert diag['decision_counts'] == {'selected': 30, 'output_cap': 30}


def test_batches_have_no_per_batch_ten_result_cap():
    rows = [candidate(i) for i in range(1, 50)]

    def model(_system, prompt, **kwargs):
        supplied_rows = supplied(prompt)
        return {'selected': [choice(int(row['candidate_id'][1:])) for row in supplied_rows
                             if int(row['candidate_id'][1:]) <= 17]}

    selected, diag = select_relevant_records(rows, **options(), max_workers=3, llm_json=model)
    assert len(selected) == 17
    assert diag['selector_batching']['batch_sizes'] == [17, 16, 16]
    assert {row['selection_batch'] for row in selected} == {1}


def test_failed_batch_is_partial_and_other_batches_remain_traceable():
    def model(system, prompt, **kwargs):
        if supplied(prompt)[0]['candidate_id'] == 'R21':
            raise RuntimeError('mock provider failure')
        return select_all(system, prompt, **kwargs)

    selected, diag = select_relevant_records([candidate(i) for i in range(1, 61)], **options(), max_workers=3, llm_json=model)
    assert len(selected) == 30
    assert diag['selection_status'] == 'partial'
    assert diag['selector_batching']['failed_batch_ids'] == [2]
    assert len(diag['candidate_decisions']) == 60
    assert not any(21 <= int(row['selection_candidate_id'][1:]) <= 40 for row in selected)


def refs():
    return [{'id':f'B{i}', 'candidate_title':f'Sparse Key Value Memory for Recommendation {i}',
             'publication_year':2023, 'authors':['A. Researcher']} for i in range(1, 6)]


def test_neighbor_expansion_uses_three_verified_seeds_and_preserves_direction():
    resolutions = []
    expansions = []

    def resolve(title, **kwargs):
        resolutions.append(title)
        return {'status':'matched', 'resolved_pub_id':f'{len(resolutions):024x}', 'api_calls':[], 'cost':{}}

    def relations(pub_id, **kwargs):
        expansions.append(pub_id)
        return {'status':'completed', 'target_references':[candidate(80, relation_role='target_references',
                  local_citation_match={'coverage_status':'already_cited'}, cutoff_status='admissible_via_local_citation',
                  published='2024')], 'summary':{'relation_api_calls_attempted':1},
                'cost':{'estimated_total_yuan':0.1}, 'api_calls':[]}

    records, diag = expand_reference_neighbors(
        refs(), known_cited_controls=[], bibliography_ready=True, queries=[QUERY], local_title='A New Recommendation Study',
        review_as_of='2024-11-26', api_key='mock', proxy=None, resolve=resolve, fetch_relations=relations,
    )
    assert len(resolutions) == len(expansions) == 3
    assert diag['estimated_cost_yuan'] == 0.3
    assert diag['raw_neighbor_count'] == 3
    for row in records:
        assert row['relation_role'] == 'reference_neighbor'
        assert row['relation_evidence']['direction'] == 'seed_cites_candidate'
        assert 'local_citation_match' not in row
        assert 'cutoff_status' not in row
        assert 'missing_status' not in row
    selected, diag = retrieve_related_work(
        [QUERY], local_title='A New Recommendation Study', review_as_of='2024-11-26', proxy=None,
        seed_records=records, aminer_api_key='mock', bibliography_ready=True, local_references=refs(),
        aminer_search_fn=lambda *args, **kwargs: ([], {'api_calls':[], 'provider_errors':[], 'cost':{}}),
        select_relevant=lambda rows, **kwargs: select_relevant_records(rows, **kwargs, llm_json=select_all),
    )
    assert selected == []  # Seed-citation evidence cannot resolve B's same-year date.
    assert diag['already_cited_control_count'] == 0
    assert diag['same_year_date_uncertain_records'] == 1


def test_uncited_neighbors_survive_but_actual_manuscript_citations_are_controls():
    new = candidate(81, relation_role='reference_neighbor', retrieval_role='reference_neighbor')
    cited = candidate(82, title=refs()[0]['candidate_title'], relation_role='reference_neighbor', retrieval_role='reference_neighbor')
    selected, diag = retrieve_related_work(
        [QUERY], local_title='A New Recommendation Study', review_as_of='2024-11-26', proxy=None,
        seed_records=[new, cited], aminer_api_key='mock', bibliography_ready=True, local_references=refs(),
        aminer_search_fn=lambda *args, **kwargs: ([], {'api_calls':[], 'provider_errors':[], 'cost':{}}),
        select_relevant=lambda rows, **kwargs: select_relevant_records(rows, **kwargs, llm_json=select_all),
    )
    assert [row['aminer_pub_id'] for row in selected] == [new['aminer_pub_id']]
    assert selected[0]['missing_status'] == 'uncited'
    assert diag['already_cited_control_count'] == 1


@pytest.mark.parametrize('status', ['ambiguous', 'not_found', 'insufficient_identity_evidence'])
def test_unverified_seed_never_triggers_relation_expansion(status):
    def forbidden(*args, **kwargs):
        raise AssertionError('Unverified identity must not be expanded')
    records, diag = expand_reference_neighbors(
        refs(), known_cited_controls=[], bibliography_ready=True, queries=[QUERY], local_title='Recommendation',
        review_as_of='2024-11-26', api_key='mock', proxy=None,
        resolve=lambda *args, **kwargs: {'status':status}, fetch_relations=forbidden,
    )
    assert records == []
    assert all(row['status'] == 'identity_not_verified' for row in diag['seeds'])


def test_private_trace_is_valid_under_concurrent_large_writes(tmp_path):
    trace = AuditTrace(tmp_path/'trace.jsonl', secrets=('sensitive-key',))
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda i: trace({'index':i, 'text':'sensitive-key' + 'x'*100000}), range(30)))
    rows = [json.loads(line) for line in trace.path.read_text().splitlines()]
    assert {row['index'] for row in rows} == set(range(30))
    assert 'sensitive-key' not in trace.path.read_text()


def test_grobid_reference_has_identity_evidence_without_guessing_year_from_title():
    tei = '''<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><back><listBibl><biblStruct xml:id="b0">
    <analytic><title level="a" type="main">Recommendation for Year 2020 Users</title>
    <author><persName><forename>Alice</forename><surname>Researcher</surname></persName></author></analytic>
    <monogr><imprint><date type="published" when="2023-05"/></imprint></monogr>
    </biblStruct></listBibl></back></text></TEI>'''
    ledger = build_reference_ledger_from_grobid(tei)
    row = ledger['records'][0]
    assert row['publication_year'] == 2023
    assert row['authors']
