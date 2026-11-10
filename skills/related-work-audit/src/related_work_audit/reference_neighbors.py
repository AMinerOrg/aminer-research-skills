"""Bounded backward expansion from manuscript-cited, verified seed papers."""

from __future__ import annotations

import time
from typing import Any, Callable

from .aminer import normalize_pub_id
from .aminer_search import normalize_title, resolve_title_to_pub_id
from .relation import fetch_aminer_relation_context
from .retrieval import facet_alignment, manuscript_profile_from_queries, meaningful_tokens, partition_relation_references


def expand_reference_neighbors(
    local_references: list[dict[str, Any]], *,
    known_cited_controls: list[dict[str, Any]], bibliography_ready: bool,
    queries: list[Any], local_title: str, review_as_of: str,
    api_key: str, proxy: str | None, seed_limit: int = 3,
    resolve: Callable[..., dict[str, Any]] = resolve_title_to_pub_id,
    fetch_relations: Callable[..., dict[str, Any]] = fetch_aminer_relation_context,
    trace: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not 0 <= seed_limit <= 3:
        raise ValueError("Reference expansion permits at most three seed papers.")
    started = time.monotonic()
    profile = manuscript_profile_from_queries(queries)
    terms = meaningful_tokens(" ".join(term for values in profile.values() for term in values))
    terms |= meaningful_tokens(local_title)
    _, confirmed_controls = partition_relation_references(
        known_cited_controls, local_references=local_references,
        bibliography_ready=bibliography_ready,
    )
    controls = {normalize_title(str(item.get('title') or '')): item for item in confirmed_controls}
    inventory = list(local_references) if bibliography_ready else []
    ranked = []
    seen_titles: set[str] = set()
    for item in inventory:
        title = str(item.get('candidate_title') or item.get('title') or '').strip()
        key = normalize_title(title)
        if not item.get('resolution_eligible', True) or not key or key in seen_titles:
            continue
        seen_titles.add(key)
        control = controls.get(key, {})
        aligned = facet_alignment({'title': title, 'summary': control.get('summary', '')}, profile)
        overlap = meaningful_tokens(title) & terms
        score = int(aligned['score']) + 2 * len(overlap)
        years = {int(year) for year in item.get('year_candidates', []) if str(year).isdigit() and 1000 <= int(year) <= 9999}
        explicit_year = item.get('publication_year') or item.get('year')
        year = int(explicit_year) if str(explicit_year or '').isdigit() else next(iter(years)) if len(years) == 1 else None
        if score <= 0 or (year and year > int(review_as_of[:4])):
            continue
        ranked.append({'title': title, 'score': score, 'overlap': sorted(overlap),
                       'reference': item, 'control': control, 'year': year})
    ranked.sort(key=lambda item: (-item['score'], normalize_title(item['title'])))
    diagnostics: dict[str, Any] = {
        'status': 'disabled' if not seed_limit else 'completed',
        'maximum_depth_from_seed': 1, 'seed_limit': seed_limit,
        'eligible_seed_count': len(ranked), 'seeds': [], 'api_calls': [],
        'raw_neighbor_count': 0, 'relation_calls': 0,
        'estimated_cost_yuan': 0.0,
        'policy': 'manuscript cites seed; seed cites candidate; candidate citation status checked against manuscript separately',
    }
    candidates: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    # No recursive traversal and no extra attempts beyond the chosen seed slots.
    for seed in ranked[:seed_limit]:
        item = seed['reference']
        info = {key: seed[key] for key in ('title', 'score', 'overlap', 'year')}
        diagnostics['seeds'].append(info)
        pub_id = str(seed['control'].get('aminer_pub_id') or '')
        try:
            if pub_id:
                pub_id = normalize_pub_id(pub_id)
                info['identity_source'] = 'verified_manuscript_backward_reference'
            else:
                identity = resolve(seed['title'], authors=item.get('authors') or [],
                                   manuscript_year=seed['year'], review_as_of=review_as_of,
                                   api_key=api_key, proxy=proxy)
                info['identity_resolution'] = identity
                diagnostics['api_calls'].extend(identity.get('api_calls') or [])
                diagnostics['estimated_cost_yuan'] += float((identity.get('cost') or {}).get('estimated_total_yuan') or 0)
                if identity.get('status') != 'matched':
                    info['status'] = 'identity_not_verified'
                    continue
                pub_id = normalize_pub_id(str(identity.get('resolved_pub_id') or ''))
                info['identity_source'] = 'exact_title_and_bibliography_metadata'
            if pub_id in seen_ids:
                info['status'] = 'duplicate_seed_id'
                continue
            seen_ids.add(pub_id)
            info['seed_pub_id'] = pub_id
            result = fetch_relations(pub_id, review_as_of=review_as_of, api_key=api_key, proxy=proxy)
            info['status'] = str(result.get('status') or 'unknown')
            diagnostics['relation_calls'] += int((result.get('summary') or {}).get('relation_api_calls_attempted') or 0)
            diagnostics['api_calls'].extend(result.get('api_calls') or [])
            diagnostics['estimated_cost_yuan'] += float((result.get('cost') or {}).get('estimated_total_yuan') or 0)
            neighbors = []
            for record in result.get('target_references') or []:
                # A seed's bibliography is NOT the manuscript's bibliography.
                # Never propagate target_references or a local-citation cutoff exception.
                clean = {key: value for key, value in record.items() if key not in {
                    'relation_role', 'relation_evidence', 'local_citation_match', 'missing_status',
                    'cutoff_status', 'time_admissibility', 'date_evidence',
                }}
                clean.update(
                    relation_role='reference_neighbor', retrieval_role='reference_neighbor',
                    query_role='reference_neighbor', query=f"References of {seed['title']}",
                    relation_evidence={'direction': 'seed_cites_candidate',
                                       'seed_pub_id': pub_id, 'seed_title': seed['title'],
                                       'candidate_pub_id': record.get('aminer_pub_id'),
                                       'manuscript_cites_seed': True, 'depth_from_seed': 1},
                )
                neighbors.append(clean)
            info['raw_neighbor_count'] = len(neighbors)
            candidates.extend(neighbors)
            if trace:
                trace({'event': 'reference_neighbor_seed', 'seed': info, 'records': neighbors,
                       'relation_diagnostics': {key: value for key, value in result.items() if key != 'target_references'}})
        except Exception as exc:
            info.update(status='failed', error_type=type(exc).__name__)
    diagnostics['raw_neighbor_count'] = len(candidates)
    if any(item.get('status') in {'partial', 'failed', 'unknown'} for item in diagnostics['seeds']):
        diagnostics['status'] = 'partial'
    diagnostics['elapsed_seconds'] = round(time.monotonic() - started, 3)
    diagnostics['estimated_cost_yuan'] = round(diagnostics['estimated_cost_yuan'], 4)
    return candidates, diagnostics
