"""Regressions for lost candidates, unsupported transfers, and replay evidence."""

import copy
import json
import stat
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from related_work_audit import __version__
from related_work_audit.aminer_search import (
    AMINER_PAPER_SEARCH_URL, AMINER_PAPER_INFO_URL, AMINER_PAPER_DETAIL_URL,
    _published_value, retrieve_aminer_records,
)
from related_work_audit.audit_trace import AuditTrace
from related_work_audit.config import ModelProfile
from related_work_audit.coverage import assess_related_work_coverage
from related_work_audit.model import llm_json
from related_work_audit.pipeline import _validate, run_audit
from related_work_audit.planning import RoutedQuery, select_relevant_records
from related_work_audit.retrieval import filter_retrieval_records, retrieve_related_work


PROFILE = {
    "domain_terms": ["sequential recommendation"],
    "task_terms": ["user preference prediction"],
    "mechanism_terms": ["sparse key value memory"],
    "data_protocol_terms": ["long user history"],
}
QUERY = RoutedQuery(
    "sparse memory recommendation", query_shape="keyword_phrase",
    fallback_phrase="sparse memory recommendation", role="method_mechanism",
    pro_field="keyword", retrieval_profile=PROFILE,
)


def candidate(index=1, **overrides):
    return {
        "title": f"Sequential Recommendation with Sparse Key Value Memory Variant {index}",
        "published": "2023-03-01",
        "summary": "Sequential recommendation for user preference prediction uses sparse key value memory and long user history.",
        "query": str(QUERY), "retrieval_role": "method_mechanism",
        "query_role": "method_mechanism", "relevance_score": "7",
        "source": "AMiner Open Platform", "aminer_pub_id": f"{index:024x}",
        "url": f"https://www.aminer.cn/pub/{index:024x}", "missing_status": "uncited",
        "facet_alignment": {"profile_available": True, "task_domain_fit": True,
                            "publication_fit": True, "task_aligned": True,
                            "mechanism_aligned": True, "data_protocol_aligned": True},
        **overrides,
    }


def choice(index=1, **overrides):
    return {"candidate_id": f"R{index}", "relevance": "high", "comparison_role": "method_precedent",
            "reason": "The supplied abstract studies sparse key value memory for sequential recommendation.", **overrides}


def transfer_choice(**overrides):
    return choice(relation_scope="cross_domain_transfer", comparison_role="direct_baseline", transfer={
        "basis": "mechanism", "manuscript_facet": "sparse key value memory",
        "manuscript_quote": "We compress long user histories using sparse key value memory.",
        "candidate_quote": "Sparse key value memory enables selective storage",
        "explanation": "Selective storage could inform how the manuscript compresses long user histories.",
        "limitation": "Language modeling evaluates token prediction, not recommendation ranking.",
        **overrides,
    })


def cross_candidate(**overrides):
    return candidate(**{
        "title": "Selective Storage for Language Modeling",
        "summary": "Sparse key value memory enables selective storage in language modeling.",
        "relevance_score": "1",
        "facet_alignment": {"profile_available": True, "task_domain_fit": False,
                         "publication_fit": False, "mechanism_only_cross_domain": True},
        **overrides,
    })


def select(rows, selected, context="We compress long user histories using sparse key value memory."):
    return select_relevant_records(rows, local_title="A New Recommendation Study", queries=[QUERY],
                                   review_as_of="2024-11-26", manuscript_context=context,
                                   llm_json=lambda *args, **kwargs: {"selected": selected})


def test_sixty_candidates_and_thirty_results_with_complete_decisions():
    rows = [candidate(i) for i in range(1, 81)]
    captured = {}

    def model(system, user, **kwargs):
        captured.update(prompt=user, **kwargs)
        # A late core record must not be lost to 30 earlier extended records.
        return {"selected": [choice(i, relevance="medium" if i <= 30 else "high") for i in range(1, 61)]}

    selected, diagnostics = select_relevant_records(
        rows, local_title="New Manuscript", queries=[QUERY], review_as_of="2024-11-26",
        manuscript_context="The proposed controller stores user preference trajectories.", llm_json=model,
    )
    assert len(selected) == 30
    assert all(row["tier"] == "core" for row in selected)
    assert selected[-1]["aminer_pub_id"] == f"{60:024x}"
    assert diagnostics["selection_candidate_count"] == 60
    assert diagnostics["shortlist_cap_removed_count"] == 20
    assert diagnostics["decision_counts"] == {"output_cap": 30, "selected": 30}
    assert len({item["candidate_id"] for item in diagnostics["candidate_decisions"]}) == 60
    assert "stores user preference trajectories" in captured["prompt"]
    assert captured["max_tokens"] >= 6000
    audit = assess_related_work_coverage(selected, {"status": "ready_for_resolution", "records": [{"id": "B1", "candidate_title": "Optical Wave Propagation in Crystalline Materials"}]})
    assert len(audit["records"]) == 30
    assert audit["summary"]["evaluation_view_limit"] == 30
    assert audit["summary"]["partition_conservation"]["conserved"]


def test_grounded_transfer_is_extended_and_retains_limits_in_final_report():
    selected, diagnostics = select([cross_candidate()], [transfer_choice()])
    assert len(selected) == 1
    assert selected[0]["tier"] == "extended"
    assert selected[0]["comparison_role"] == "method_precedent"
    assert diagnostics["cross_domain_transfer_count"] == 1
    audit = assess_related_work_coverage(selected, {"status": "ready_for_resolution", "records": []})
    assert audit["records"][0]["transfer_evidence"]["evidence"]["limitation"]
    assert audit["records"][0]["relation_scope"] == "cross_domain_transfer"


@pytest.mark.parametrize("bad_evidence", [
    {"candidate_quote": "This passage was never supplied in the abstract."},
    {"manuscript_quote": "This passage was never supplied in the manuscript."},
    {"manuscript_quote": ""},
    {"manuscript_facet": "memory"}, {"basis": "generic_similarity"}, {"limitation": ""}, {"explanation": ""},
])
def test_unsupported_transfer_cannot_bypass_domain_gate(bad_evidence):
    selected, diagnostics = select([cross_candidate()], [transfer_choice(**bad_evidence)])
    assert selected == []
    assert diagnostics["low_confidence_excluded_count"] == 1
    assert len(diagnostics["candidate_decisions"]) == 1


@pytest.mark.parametrize("overrides", [
    {"published": "2025-01-01"}, {"published": "2024"},
    {"missing_status": "already_cited"}, {"local_citation_match": {"coverage_status": "already_cited"}},
    {"aminer_pub_id": "", "url": ""}, {"title": "A New Recommendation Study"},
])
def test_grounded_transfer_never_bypasses_date_citation_identity_or_self_checks(overrides):
    selected, diagnostics = select([cross_candidate(**overrides)], [transfer_choice()])
    assert selected == []
    assert diagnostics["low_confidence_excluded_count"] == 1


def test_same_domain_task_precedent_does_not_need_same_mechanism():
    row = candidate()
    row["facet_alignment"].update(mechanism_aligned=False, data_protocol_aligned=False)
    selected, _ = select([row], [choice(comparison_role="task_precedent")])
    assert selected[0]["tier"] == "core"


def test_specific_profile_match_does_not_need_an_extra_title_word_score():
    row = candidate(relevance_score="1", has_distinctive_title_overlap=False)
    selected, _ = select([row], [choice(comparison_role="alternative_formulation")])
    assert len(selected) == 1
    assert selected[0]["verification"]["relevance"]["profile_topical_evidence"]
    # The same metadata alone never causes unselected candidates to be emitted.
    assert not select([row], [])[0]
    row["facet_alignment"].update(publication_fit=False)
    assert not select([row], [choice()])[0]


def test_verified_domain_explanation_can_supply_the_missing_top_level_reason():
    manuscript = "Sequential recommendation predicts user preferences from long histories."
    row = candidate()
    item = choice(reason="", relation_scope="same_domain", domain_evidence={
        "primary_domain": "sequential recommendation",
        "manuscript_quote": manuscript,
        "candidate_quote": row["summary"],
        "explanation": "The alternative sparse memory design can contextualize the manuscript's history compression.",
    })
    selected, diag = select([row], [item], manuscript)
    assert len(selected) == 1 and not diag["selection_schema_errors"]
    assert selected[0]["selection_reason"] == item["domain_evidence"]["explanation"]
    assert selected[0]["selection_reason_source"] == "verified_domain_explanation"
    item["domain_evidence"]["candidate_quote"] = "This fabricated sentence is not in the supplied abstract."
    rejected, diag = select([row], [item], manuscript)
    assert not rejected
    assert diag["candidate_decisions"][0]["disposition"] == "invalid_model_selection"


def test_verified_transfer_explanation_can_supply_reason_but_not_override_cutoff():
    item = transfer_choice()
    item.pop("reason")
    selected, _ = select([cross_candidate()], [item])
    assert len(selected) == 1 and selected[0]["tier"] == "extended"
    assert selected[0]["selection_reason_source"] == "verified_transfer_explanation"
    assert not select([cross_candidate(published="2025")], [item])[0]
    item["transfer"]["candidate_quote"] = "This fabricated sentence is not in the supplied abstract."
    assert not select([cross_candidate()], [item])[0]


def test_domain_anchor_accepts_reworded_label_but_still_requires_both_quotes():
    manuscript = "Sequential recommendation predicts user preferences from long histories."
    row = candidate(relevance_score="0", facet_alignment={
        "profile_available": True, "task_domain_fit": False, "publication_fit": False})
    item = choice(relation_scope="same_domain", domain_evidence={
        "primary_domain_id": "D1", "primary_domain": "recommendation from interaction histories",
        "manuscript_quote": manuscript, "candidate_quote": row["summary"],
        "explanation": "An alternative memory design contextualizes history compression.",
    })
    selected, _ = select([row], [item], manuscript)
    assert len(selected) == 1 and selected[0]["tier"] == "extended"
    assert selected[0]["same_domain_evidence"]["profile_anchor"]["text"] == "sequential recommendation"
    for bad_id in ("D99", "M1", "E1"):
        item["domain_evidence"]["primary_domain_id"] = bad_id
        assert not select([row], [item], manuscript)[0]
    item["domain_evidence"]["primary_domain_id"] = "D1"
    item["domain_evidence"]["candidate_quote"] = "Unsupported candidate properties fabricated for this test."
    assert not select([row], [item], manuscript)[0]


def test_transfer_anchor_requires_a_transfer_facet_and_cannot_override_date():
    item = transfer_choice(manuscript_facet_id="M1", manuscript_facet="selective stores of key-value memories")
    assert len(select([cross_candidate()], [item])[0]) == 1
    assert not select([cross_candidate(published="2025")], [item])[0]
    item["transfer"]["manuscript_facet_id"] = "D1"
    assert not select([cross_candidate()], [item])[0]
    item["transfer"]["manuscript_facet_id"] = "M1"
    for bad_quote in ("", "This passage was never supplied in the manuscript."):
        item["transfer"]["manuscript_quote"] = bad_quote
        selected, diag = select([cross_candidate()], [item])
        assert not selected
        assert "verbatim_manuscript_evidence" in diag["candidate_decisions"][0]["verification"]["transfer_verification"]["failed_checks"]


def test_same_domain_mechanism_precedent_is_not_mislabeled_cross_domain():
    rows, _ = filter_retrieval_records(
        [candidate(summary="Sparse key value memory for sequential recommendation.")],
        local_title="New Manuscript", review_as_of="2024-11-26", manuscript_profile=PROFILE,
    )
    assert rows[0]["facet_alignment"]["task_domain_fit"]
    assert not rows[0]["facet_alignment"]["task_aligned"]
    assert not rows[0]["facet_alignment"]["mechanism_only_cross_domain"]
    selected, _ = select(rows, [choice()])
    assert selected[0]["tier"] == "core"
    assert selected[0]["relation_scope"] == "same_domain"


def test_invalid_model_entries_are_observable_without_duplicate_dispositions():
    selected, diagnostics = select(
        [candidate(), cross_candidate()],
        [None, choice(100), choice(reason=""), choice(), choice(), choice(2)],
    )
    assert len(selected) == 1
    assert len(diagnostics["selection_schema_errors"]) == 4
    assert diagnostics["selection_status"] == "completed_with_schema_errors"
    assert len(diagnostics["candidate_decisions"]) == 2
    assert sum(diagnostics["decision_counts"].values()) == 2


@pytest.mark.parametrize("first_date", ["2025-01-01", "2024", ""])
@pytest.mark.parametrize("earlier_date", ["2024-05-01", "2023"])
def test_earlier_same_title_version_survives_future_uncertain_or_undated_first_hit(first_date, earlier_date):
    rows = [candidate(published=first_date), candidate(2, title=candidate()["title"], published=earlier_date)]
    kept, diagnostics = filter_retrieval_records(rows, local_title="New Manuscript", review_as_of="2024-11-26")
    assert len(kept) == 1
    assert kept[0]["aminer_pub_id"] == f"{2:024x}"
    assert kept[0]["published"] == earlier_date
    assert len(kept[0]["same_title_versions"]) == 2
    assert diagnostics["duplicate_records_removed"] == 1
    assert {item["disposition"] for item in diagnostics["filter_decisions"]} == {"kept", "duplicate_or_empty_title"}


def test_filter_budget_and_ledger_conserve_all_raw_rows():
    rows = [candidate(i) for i in range(1, 94)]
    rows.extend([candidate(95, published="2025-01-01"), candidate(96, published="")])
    kept, diagnostics = filter_retrieval_records(rows, local_title="New Manuscript", review_as_of="2024-11-26")
    assert len(kept) == 80
    assert diagnostics["preselection_cap_removed_count"] == 13
    assert len(diagnostics["filter_decisions"]) == 95
    assert all(item["disposition"] != "pending" for item in diagnostics["filter_decisions"])


@pytest.mark.parametrize("search_date,info_date,expected", [
    ("2024-05-01", "2025", "2024-05-01"),
    ("2024", "2025-06-01", "2024"),
    ("2024-12-01", "2024-05-01", "2024-05-01"),
])
def test_aminer_enrichment_preserves_search_abstract_and_date_evidence(search_date, info_date, expected):
    pub_id = "1" * 24
    abstract = "Sparse key value memory for sequential recommendation. " * 30

    def fields(value):
        return {"year": value} if len(value) == 4 else {"publication_date": value}

    def handler(request):
        url = str(request.url).split("?")[0]
        if url == AMINER_PAPER_SEARCH_URL:
            data = [{"id": pub_id, "title": "Sparse Key Value Memory for Sequential Recommendation", "abstract": abstract, **fields(search_date)}]
        elif url == AMINER_PAPER_INFO_URL:
            data = [{"id": pub_id, "abstract_slice": "Sparse memory.", **fields(info_date)}]
        else:
            raise AssertionError(f"unexpected route {url}")
        return httpx.Response(200, json={"code": 200, "data": data})

    def factory(**kwargs):
        kwargs.pop("proxy", None)
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    rows, diagnostics = retrieve_aminer_records(
        [str(QUERY)], review_as_of="2024-11-26", api_key="test-key", proxy=None,
        search_tier="free", detail_limit=0, client_factory=factory,
    )
    assert rows[0]["summary"] == abstract.strip()
    assert rows[0]["published"] == expected
    assert len(rows[0]["publication_date_evidence"]) == 2
    assert diagnostics["cost"]["estimated_total_yuan"] == 0
    assert _published_value({"publication_date": "2024-99-31", "year": "2023"}) == "2023"


def test_model_trace_records_truncation_usage_and_redacts_credentials(tmp_path):
    usage = SimpleNamespace(model_dump=lambda: {"prompt_tokens": 100, "completion_tokens": 900, "total_tokens": 1000})
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"selected":[]}'), finish_reason="length")], usage=usage)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: response)))
    profile = ModelProfile("glm53", "GLM", "glm-5.3", "test-secret-token", "https://example.invalid", thinking_effort="low")
    trace = AuditTrace(tmp_path / "trace.jsonl", secrets=(profile.api_key,))
    with patch("related_work_audit.model.OpenAI") as constructor:
        constructor.return_value.__enter__.return_value = client
        with pytest.raises(ValueError, match="token limit"):
            llm_json(profile, "system", "input test-secret-token", max_tokens=6000, audit=trace)
    text = trace.path.read_text()
    events = [json.loads(line) for line in text.splitlines()]
    assert "test-secret-token" not in text
    assert stat.S_IMODE(trace.path.stat().st_mode) == 0o600
    assert [event["event"] for event in events] == ["model_response", "model_output_rejected"] * 2
    assert events[0]["usage"]["total_tokens"] == 1000
    assert events[0]["finish_reason"] == "length"
    assert events[0]["request"]["reasoning_effort"] == "low"
    assert events[0]["request"]["temperature"] == 0.15


def test_twenty_five_mib_pdf_boundary(tmp_path):
    pdf = tmp_path / "paper.pdf"
    with pdf.open("wb") as stream:
        stream.truncate(25 * 1024 * 1024)
    _validate(pdf, None, None, "2024-11-26", allow_pypdf_fallback=False)
    with pdf.open("ab") as stream:
        stream.write(b"x")
    with pytest.raises(ValueError, match="25 MiB"):
        _validate(pdf, None, None, "2024-11-26", allow_pypdf_fallback=False)


def test_pipeline_uses_independent_selector_and_retains_replay_evidence(tmp_path):
    pdf = tmp_path / "manuscript.pdf"
    pdf.write_bytes(b"mock-pdf")
    text = "Sequential recommendation uses sparse key value memory to predict user preferences. " * 200
    parsed = {"pages": [text], "markdown": text, "local_title": "A New Recommendation Study",
              "provider": "grobid", "source_locations": [], "physical_pages": [text]}
    profile = ModelProfile("glm53", "GLM", "glm-5.3", "private-model-token", "https://example.invalid", thinking_effort="low")
    rows = [candidate(i, published="2024" if i % 2 else "2024-12-15") for i in range(1, 16)]

    def retriever(queries, **kwargs):
        assert kwargs["review_as_of"] == "2024-12-31"
        return retrieve_related_work(queries, **kwargs, aminer_search_fn=lambda *a, **kw: (copy.deepcopy(rows), {"api_calls": [], "cost": {}, "provider_errors": []}))

    response = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=json.dumps({"selected": [choice(i) for i in range(1, 16)]})), finish_reason="stop")], usage=None)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: response)))
    with (
        patch("related_work_audit.pipeline.prepare_uploaded_pdf_source", return_value=parsed),
        patch("related_work_audit.pipeline.build_reference_ledger_for_pdf", return_value={"status": "ready_for_resolution", "records": [{"id": "B1", "candidate_title": "Optical Wave Propagation in Crystalline Materials"}]}),
        patch("related_work_audit.pipeline.resolve_title_to_pub_id", return_value={"status": "not_found"}),
        patch("related_work_audit.pipeline.aminer_api_key", return_value="private-aminer-token"),
        patch("related_work_audit.pipeline.build_related_work_queries", return_value=[QUERY]),
        patch("related_work_audit.pipeline.build_feedback_queries", return_value=([], {})),
        patch("related_work_audit.pipeline.retrieve_related_work", side_effect=retriever),
        patch("related_work_audit.model.OpenAI") as constructor,
    ):
        constructor.return_value.__enter__.return_value = client
        manifest = run_audit(pdf=pdf, review_as_of="2024-11-26", publication_year_grace=0,
                             profile=profile, work_root=tmp_path / "runs", output=None, force=False)
    from pathlib import Path
    report_path = Path(manifest["report"])
    report = json.loads(report_path.read_text())
    metadata = report["run_metadata"]
    assert metadata["publication_cutoff_policy"] == "inclusive_year"
    assert metadata["publication_cutoff_year"] == 2024
    assert metadata["requested_review_as_of"] == "2024-11-26"
    assert metadata["review_as_of"] == "2024-12-31"
    assert metadata["skill_version"] == __version__
    assert metadata["entrypoint"] == "related_work_audit.cli"
    assert len(metadata["source_sha256"]) == 64
    records = report["results"]["related_work_coverage"]["records"]
    assert len(records) == 15
    trace_text = (report_path.parent / metadata["audit_trace_file"]).read_text()
    assert "private-model-token" not in trace_text and "private-aminer-token" not in trace_text
    events = [json.loads(line) for line in trace_text.splitlines()]
    assert {event["event"] for event in events} >= {"run_started", "aminer_search_round", "retrieval_filter", "model_response", "selection"}
    assert next(event for event in events if event["event"] == "selection")["diagnostics"]["manuscript_context_chars"] > 0
