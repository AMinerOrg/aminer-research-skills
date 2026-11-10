"""Source-backed task precedents and the user-facing inclusive-year policy."""
import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from related_work_audit.cli import build_parser
from related_work_audit.pipeline import _validate, resolve_reference_seed_limit
from related_work_audit.planning import RoutedQuery, select_relevant_records
from related_work_audit.publication_time import review_year_end
from related_work_audit.retrieval import filter_retrieval_records
from related_work_audit.candidate_pool import semantic_candidate_pool
from related_work_audit.relation import _time_fields


def fixture():
    return json.loads((Path(__file__).parent / 'fixtures/task_precedent.json').read_text())


def select(data):
    return select_relevant_records([data['candidate']], local_title=data['local_title'],
        queries=[RoutedQuery('conversational question answering', retrieval_profile=data['profile'],
            query_shape='keyword_phrase', fallback_phrase='conversational question answering',
            role='problem_setting', pro_field='keyword')],
        review_as_of='2024-12-31', manuscript_context=data['manuscript_context'],
        llm_json=lambda *a, **kw: {'selected': [data['source_backed_choice']]})


def test_task_precedent_with_bad_lexical_alignment_reaches_report():
    data = fixture()
    original = copy.deepcopy(data)
    rows, diagnostics = select(data)
    assert len(rows) == 1 and rows[0]['tier'] == 'extended'
    assert rows[0]['comparison_role'] == 'task_precedent'
    assert rows[0]['transfer_evidence']['profile_anchor']['id'] == 'T2'
    assert diagnostics['candidate_decisions'][0]['disposition'] == 'selected'
    assert data == original


@pytest.mark.parametrize('fault', ['missing_quote', 'fabricated_quote', 'bad_anchor',
    'generic_facet', 'no_explanation', 'no_limitation', 'future', 'cited', 'self'])
def test_task_precedent_preserves_evidence_and_metadata_boundaries(fault):
    data = fixture()
    evidence = data['source_backed_choice']['transfer']
    if fault == 'missing_quote': evidence.pop('manuscript_quote')
    elif fault == 'fabricated_quote': evidence['candidate_quote'] = 'This dataset evaluates long-term chat memory.'
    elif fault == 'bad_anchor': evidence['manuscript_facet_id'] = 'D1'
    elif fault == 'generic_facet': evidence['manuscript_facet'] = 'task'
    elif fault == 'no_explanation': evidence['explanation'] = ''
    elif fault == 'no_limitation': evidence['limitation'] = ''
    elif fault == 'future': data['candidate']['published'] = '2025'
    elif fault == 'cited': data['candidate']['missing_status'] = 'already_cited'
    elif fault == 'self': data['candidate']['title'] = data['local_title']
    assert select(data)[0] == []


def test_task_transfer_cannot_be_promoted_to_direct_baseline():
    data = fixture()
    data['source_backed_choice']['comparison_role'] = 'direct_baseline'
    rows, _ = select(data)
    assert rows[0]['comparison_role'] == 'task_precedent'


@pytest.mark.parametrize('value', ['2024', '2024-01-01', '2024-11-26', '2024-12-31'])
def test_cli_and_programmatic_entry_use_the_entire_requested_year(value):
    args = build_parser().parse_args(['--pub-id', '0' * 24, '--review-as-of', value])
    assert _validate(None, args.pub_id, None, args.review_as_of,
                     allow_pypdf_fallback=False)[3] == '2024-12-31'
    assert review_year_end(value) == '2024-12-31'


def test_new_cli_year_flag_defaults_to_current_year():
    args = build_parser().parse_args(['--pub-id', '0' * 24])
    assert args.review_as_of == str(datetime.now(timezone.utc).year)
    assert args.publication_year_grace == 1
    args = build_parser().parse_args(['--pub-id', '0' * 24, '--review-year', '2024'])
    assert args.review_as_of == '2024'
    assert review_year_end(args.review_as_of, args.publication_year_grace) == '2025-12-31'


def test_aminer_reference_seed_default_matches_evaluated_configuration():
    args = build_parser().parse_args(['--pub-id', '0' * 24])
    assert resolve_reference_seed_limit(args.search_provider, args.reference_seed_limit) == 3
    args = build_parser().parse_args(['--pub-id', '0' * 24, '--reference-seed-limit', '0'])
    assert resolve_reference_seed_limit(args.search_provider, args.reference_seed_limit) == 0


def test_external_provider_keeps_reference_expansion_disabled_by_default():
    args = build_parser().parse_args(['--pdf', '/tmp/paper.pdf', '--search-provider', 'arxiv-openalex'])
    assert resolve_reference_seed_limit(args.search_provider, args.reference_seed_limit) == 0
    with pytest.raises(ValueError, match='AMiner provider'):
        resolve_reference_seed_limit('arxiv-openalex', 3)


@pytest.mark.parametrize('value', ['2024-02-30', '2024-11', '0', '0000', '2024 junk'])
def test_invalid_year_or_legacy_date_is_not_silently_coerced(value):
    with pytest.raises(ValueError): review_year_end(value)


@pytest.mark.parametrize('published', ['2024', '2024-12', '2024-12-31'])
def test_same_year_survives_filter_and_semantic_bank_without_inventing_a_date(published):
    row = fixture()['candidate'] | {'published': published, 'cutoff_status': 'uncertain_same_year',
                                   'time_admissibility': 'uncertain'}
    cutoff = review_year_end('2024-11-26')
    filtered, _ = filter_retrieval_records([row], local_title='New work', review_as_of=cutoff)
    bank, diagnostics = semantic_candidate_pool(filtered, local_title='New work',
        review_as_of=cutoff, manuscript_profile={}, manuscript_context='',
        llm_json=lambda *a, **kw: pytest.fail('A one-record bank needs no model call'))
    assert len(bank) == 1 and diagnostics['pending_date_count'] == 0
    assert bank[0]['published'] == published
    assert bank[0]['time_admissibility'] == 'admissible'
    assert _time_fields({'year': 2024}, cutoff)['time_admissibility'] == 'admissible'


def test_later_year_is_excluded_and_absent_year_remains_unresolved():
    base = fixture()['candidate']
    for published, counter in [('2025', 'future_records_removed'), ('', 'undated_records_removed')]:
        rows, diag = filter_retrieval_records([base | {'published': published}],
            local_title='New work', review_as_of=review_year_end('2024'))
        assert rows == [] and diag[counter] == 1


@pytest.mark.parametrize('row', [{'year': 0}, {'year': -1}, {'year': 10000}, {'year': 'bad'}])
def test_relation_metadata_never_admits_an_invalid_year(row):
    result = _time_fields(row, review_year_end('2024'))
    assert result['time_admissibility'] == 'unknown'
    assert result['published'] == ''


@pytest.mark.parametrize('published,precision', [('2024', 'year'), ('2024-12', 'month')])
def test_relation_metadata_reads_valid_published_without_redundant_year(published, precision):
    result = _time_fields({'published': published}, review_year_end('2024'))
    assert result['published'] == published and result['date_precision'] == precision
    assert result['time_admissibility'] == 'admissible'
