"""Observed query corruption and evidence false-negative regressions."""
import pytest

from related_work_audit.aminer_search import _search_once_request
from related_work_audit.evidence import profile_label, verified_quote
from related_work_audit.manuscript_context import manuscript_context
from related_work_audit.planning import _routed_queries_from_payload, _transfer_evidence


def test_empty_methods_keeps_the_planned_domain_in_the_actual_api_request():
    payload = {"queries": [{
        "query": "random seed effects on pretrained language model performance",
        "role": "method_mechanism", "query_shape": "keyword_phrase", "methods": [],
        "search_terms": ["random seed", "pretraining", "language model", "performance"],
    }]}
    plan = _routed_queries_from_payload(payload, limit=10)[0]
    _, _, _, request = _search_once_request({
        "query": str(plan), "query_shape": plan.query_shape,
        "fallback_phrase": plan.fallback_phrase,
    }, "pro", 2024)
    assert request["query"] == "random seed pretraining language model performance"


def test_exact_title_with_commas_is_one_complete_lookup():
    title = "StreamingT2V: Consistent, Dynamic, and Extendable Long Video Generation from Text"
    plans = _routed_queries_from_payload({"exact_titles": [title]}, limit=10)
    assert [str(p) for p in plans] == [title]
    assert plans[0].query_shape == "exact_title"


def test_explicit_method_list_still_searches_each_individual_method():
    plans = _routed_queries_from_payload({"queries": [{
        "query": "A method comparison", "role": "direct_baseline",
        "methods": ["SimPO", "ORPO"], "search_terms": ["preference optimization"],
    }]}, limit=10)
    assert [str(p) for p in plans] == ["SimPO", "ORPO"]
    assert [p.fallback_phrase for p in plans] == ["SimPO", "ORPO"]


def test_late_related_work_and_evaluation_survive_a_long_opening():
    source = ("## Abstract\nA specific contribution to video generation.\n"
              "## Introduction\n" + "Long introduction evidence. "*1500 +
              "\n## Related Work\nA late discussion of prior noise scheduling methods.\n"
              "## Method\nThe model uses overlapping temporal attention windows.\n"
              "## Evaluation\nMeasure temporal consistency over long generated videos.\n"
              "## References\n" + "Bibliography entry. "*800)
    for limit in (10000, 16000):
        context = manuscript_context(source, limit)
        assert len(context) <= limit
        assert "A specific contribution" in context
        assert "late discussion of prior noise scheduling" in context
        assert "overlapping temporal attention windows" in context
        assert "Measure temporal consistency" in context
        assert "Bibliography entry" not in context


def test_qualified_domain_and_task_labels_do_not_require_identical_strings():
    profile = {"domain_terms": ["multimodal large language models"],
               "task_terms": ["long-term memory benchmarking"]}
    assert profile_label("efficient small multimodal large language models", profile, ("domain_terms",))
    assert profile_label("multimodal large language model training paradigms", profile, ("domain_terms",))
    assert profile_label("long-term memory benchmarking", profile, ("domain_terms", "task_terms"))
    assert not profile_label("language models", profile, ("domain_terms",))
    assert not profile_label("memory", profile, ("domain_terms", "task_terms"))


def test_nested_subsections_inherit_the_parent_context_budget():
    source = (
        "## Introduction\n" + "A lengthy opening. " * 2000 +
        "\n## Method\n### Construction\nThe mechanism uses paired stochastic samples.\n" +
        "## Evaluation\n### Confidence bins\nCalibration errors are assessed in confidence bins.\n" +
        "## Related Work\n### Prior dialogue corpora\nEarlier corpora test memory in long interactions.\n" +
        "## References\n### Primary references\nBibliography content must be excluded.\n"
    )
    for limit in (10000, 16000):
        result = manuscript_context(source, limit)
        assert len(result) <= limit
        assert "paired stochastic samples" in result
        assert "Calibration errors are assessed" in result
        assert "Earlier corpora test memory" in result
        assert "Bibliography content" not in result


def test_nonadjacent_source_spans_have_explicit_omission_boundaries():
    source = (
        "## Related Work\nFirst related discussion.\n" +
        "## Method\nIntervening method text.\n" +
        "## Prior Work\nSecond related discussion.\n" +
        "## Introduction\n" + "A lengthy opening. " * 2000
    )
    result = manuscript_context(source, 10000)
    assert "First related discussion.\n\n[... source excerpt omitted ...]\n\n## Prior Work" in result


def test_elided_quotes_keep_real_ordered_fragments_but_reject_invention():
    first = "The memory stream stores observations from interactions"
    second = "Retrieval combines recency and relevance in the ranking"
    source = first + ". More intervening source text. " + second + "."
    assert verified_quote(first + " ... " + second, source)
    assert not verified_quote(second + " ... " + first, source)
    assert not verified_quote(first + " ... Invented conclusions from this candidate", source)
    assert not verified_quote("memory ... retrieval", source)


def test_a_missing_profile_facet_needs_a_real_manuscript_passage():
    manuscript = "We evaluate answer invariance under rotations of symbolic graphics programs."
    candidate = {"title": "Invariant Visual Reasoning", "summary": "Our evaluation tests answer invariance under image transformations."}
    item = {"transfer": {
        "basis": "evaluation", "manuscript_facet": "rotation invariance testing",
        "manuscript_quote": manuscript,
        "candidate_quote": candidate["summary"],
        "explanation": "The invariance test offers a related robustness protocol.",
        "limitation": "Images differ from symbolic programs; this is an evaluation analogy.",
    }}
    profile = {"data_protocol_terms": ["SGP benchmark"]}
    passed, evidence = _transfer_evidence(item, candidate, profile, manuscript)
    assert passed and evidence["facet_evidence_source"] == "manuscript_quote"
    item["transfer"]["manuscript_quote"] = "The manuscript never contains this invented mechanism."
    assert not _transfer_evidence(item, candidate, profile, manuscript)[0]
    item["transfer"]["manuscript_quote"] = manuscript
    item["transfer"]["basis"] = "generic_similarity"
    assert not _transfer_evidence(item, candidate, profile, manuscript)[0]
