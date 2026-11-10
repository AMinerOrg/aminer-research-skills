"""Dedicated end-to-end orchestration for the standalone Related Work Skill."""

from __future__ import annotations

import json
import re
import secrets
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .aminer import download_aminer_source, normalize_pub_id
from .aminer_search import resolve_title_to_pub_id
from .audit_trace import AuditTrace, implementation_identity
from .anchors import build_anchor_catalog
from .config import ModelProfile, aminer_api_key, grobid_url, proxy_url
from .coverage import assess_related_work_coverage
from .grobid import (
    grobid_document_authors,
    grobid_document_year,
    prepare_uploaded_pdf_source,
    request_grobid_fulltext,
)
from .input_quality import assess_input_viability
from .model import llm_json
from .publication_time import review_year_end
from .limits import MAX_PDF_BYTES, EVALUATION_VIEW_LIMIT, SELECTION_SHORTLIST_LIMIT
from .planning import (
    build_feedback_queries,
    build_related_work_queries,
    select_relevant_records,
)
from .reference_pdf import build_reference_ledger_for_pdf
from .reference_neighbors import expand_reference_neighbors
from .references import build_reference_ledger
from .relation import fetch_aminer_relation_context, unavailable_relation_context
from .retrieval import filter_retrieval_records, retrieve_related_work


RETRIEVAL_POLICIES = {
    "aminer": (
        "Shape-aware AMiner Open Platform routing across title Search, Search "
        "Pro, available QA, bounded thematic expansion, free paper_info, and "
        "bounded details; no silent external-provider fallback"
    ),
    "arxiv": "Explicit arXiv-only candidate search",
    "openalex": "Explicit OpenAlex-only candidate search",
    "arxiv-openalex": "Explicit arXiv-first search with OpenAlex fallback",
}
# Backward-compatible name for callers that used the former single default.
RETRIEVAL_POLICY = RETRIEVAL_POLICIES["aminer"]
Progress = Callable[[str, str, dict[str, Any] | None, dict[str, Any] | None], None]


def _utf8_safe(value: Any) -> str:
    return str(value).encode("utf-8", errors="replace").decode("utf-8")


def _extract_local_title(pages: list[str]) -> str:
    if not pages:
        return ""
    lines = [
        re.sub(r"^#{1,6}\s+", "", line.strip())
        for line in str(pages[0] or "").splitlines()
        if line.strip()
    ]
    title_lines: list[str] = []
    for line in lines[:16]:
        lowered = line.lower()
        if lowered.startswith(("under review", "submitted to", "published as a conference paper", "arxiv:", "arxiv ")):
            continue
        if lowered.startswith(("abstract", "keywords", "introduction")):
            break
        if title_lines and ("@" in line or any(marker in line for marker in ("*", "∗", "⋆", "†"))):
            break
        if title_lines and re.search(r"\b(university|institute|laboratory|school|company)\b", lowered):
            break
        if len(re.findall(r"[A-Za-z0-9]+", line)) < 2:
            continue
        title_lines.append(line)
        if len(title_lines) == 3:
            break
    title = re.sub(r"\s+", " ", " ".join(title_lines)).strip(" .")
    return title if 6 <= len(title) <= 260 else ""


def _retrieval_reference_inputs(
    ledger: dict[str, Any],
) -> tuple[list[dict[str, Any]], bool]:
    """Return only eligible references plus an explicit whole-ledger gate."""
    references = [
        dict(item)
        for item in ledger.get("records", [])
        if isinstance(item, dict) and item.get("resolution_eligible", True)
    ]
    ready = (
        ledger.get("status") == "ready_for_resolution" and bool(references)
    )
    return references, ready


def _structured_location_metadata(pages: list[str], locations: list[dict[str, Any]]) -> dict[str, Any]:
    first = next((item for item in locations if isinstance(item, dict)), {})
    if not first:
        return {}
    unit = str(first.get("location_unit") or "markdown_block")
    numbering = str(first.get("location_numbering") or "")
    physical = numbering == "physical_page_and_section_block" or unit == "grobid_tei_block"
    return {
        "location_granularity": "physical_page_section_block_sentence" if physical else "markdown_section_block_sentence",
        "location_numbering": first.get("location_numbering", "physical_page_and_section_block" if physical else "section_local"),
        "location_unit": unit,
        "source_unit_count": len(pages),
        "physical_page_count": len(pages) if physical else None,
        "logical_text_chunks": None if physical else len(pages),
    }


def _write_json(path: Path, payload: Any, *, force: bool) -> Path:
    path = path.expanduser().resolve()
    if path.exists() and not force:
        raise ValueError(f"Output already exists; use --force to replace it: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(rendered)
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)
    return path


def _validate(
    pdf: Path | None,
    pub_id: str | None,
    relation_pub_id: str | None,
    review_as_of: str,
    *,
    allow_pypdf_fallback: bool,
) -> tuple[Path | None, str | None, str | None, str]:
    if (pdf is None) == (pub_id is None):
        raise ValueError("Provide exactly one manuscript source: --pdf or --pub-id.")
    if pdf is not None:
        pdf = pdf.expanduser().resolve()
        if not pdf.is_file() or pdf.suffix.lower() != ".pdf":
            raise ValueError(f"PDF not found or not a .pdf file: {pdf}")
        if not 0 < pdf.stat().st_size <= MAX_PDF_BYTES:
            raise ValueError("PDF must be non-empty and no larger than 25 MiB.")
    else:
        pub_id = normalize_pub_id(pub_id or "")
        if allow_pypdf_fallback:
            raise ValueError("--allow-pypdf-fallback is only valid with --pdf.")
    if relation_pub_id:
        relation_pub_id = normalize_pub_id(relation_pub_id)
        if pub_id and relation_pub_id != pub_id:
            raise ValueError(
                "--pub-id input must reuse the same ID for citation relations; "
                "do not supply a different --relation-pub-id."
            )
    elif pub_id:
        relation_pub_id = pub_id
    try:
        cutoff = review_year_end(review_as_of)
    except ValueError as exc:
        raise ValueError("--review-year must use YYYY; legacy --review-as-of also accepts YYYY-MM-DD.") from exc
    return pdf, pub_id, relation_pub_id, cutoff


def resolve_reference_seed_limit(search_provider: str, requested: int | None) -> int:
    limit = (3 if search_provider == "aminer" else 0) if requested is None else requested
    if not 0 <= limit <= 3:
        raise ValueError("Reference seed limit must be between 0 and 3.")
    if limit and search_provider != "aminer":
        raise ValueError("Reference expansion requires the AMiner provider.")
    return limit


def run_audit(
    *,
    pdf: Path | None = None,
    pub_id: str | None = None,
    relation_pub_id: str | None = None,
    review_as_of: str,
    profile: ModelProfile,
    work_root: Path,
    output: Path | None,
    force: bool,
    allow_pypdf_fallback: bool = False,
    search_provider: str = "aminer",
    aminer_search_tier: str = "auto",
    aminer_detail_limit: int = 5,
    reference_seed_limit: int | None = None,
    selector_batch_size: int = 0,
    selector_workers: int = 1,
    semantic_preselection: bool = True,
    publication_year_grace: int = 1,
    progress: Progress | None = None,
) -> dict[str, Any]:
    if search_provider not in RETRIEVAL_POLICIES:
        raise ValueError(f"Unknown search provider: {search_provider}")
    if aminer_search_tier not in {"auto", "free", "pro", "qa", "qa-pro"}:
        raise ValueError("AMiner search tier must be auto, free, pro, qa, or qa-pro.")
    if not 0 <= aminer_detail_limit <= 20:
        raise ValueError("AMiner detail limit must be between 0 and 20.")
    reference_seed_limit = resolve_reference_seed_limit(search_provider, reference_seed_limit)
    if selector_batch_size not in {0, 20} or not 1 <= selector_workers <= 3:
        raise ValueError("Selector batch size must be 0 or 20; workers must be 1 to 3.")
    requested_review_as_of = str(review_as_of)
    effective_cutoff = review_year_end(review_as_of, publication_year_grace)
    pdf, pub_id, relation_pub_id, review_as_of = _validate(
        pdf,
        pub_id,
        relation_pub_id,
        review_as_of,
        allow_pypdf_fallback=allow_pypdf_fallback,
    )
    base_review_year = int(review_as_of[:4])
    review_as_of = effective_cutoff
    work_root = work_root.expanduser().resolve()
    work_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    work_root.chmod(0o700)
    runs_dir = work_root / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    runs_dir.chmod(0o700)
    run_id = secrets.token_hex(6)
    run_dir = runs_dir / run_id
    run_dir.mkdir(mode=0o700)
    trace = AuditTrace(run_dir / "audit_trace.jsonl", secrets=(profile.api_key,))
    implementation = implementation_identity()
    cutoff_metadata = {"publication_cutoff_policy": "inclusive_year",
                       "base_review_year": base_review_year,
                       "publication_year_grace": publication_year_grace,
                       "publication_cutoff_year": int(review_as_of[:4]),
                       "requested_review_as_of": requested_review_as_of,
                       "review_as_of": review_as_of}
    trace({"event": "run_started", **implementation, **cutoff_metadata})

    def emit(phase: str, message: str, metrics: dict[str, Any] | None = None, artifacts: dict[str, Any] | None = None) -> None:
        if progress:
            progress(phase, message, metrics, artifacts)

    grobid_request = lambda path: request_grobid_fulltext(path, grobid_url())
    local_pdf: Path | None = None
    source_manifest: dict[str, Any] = {}
    source_aminer_key: str | None = None
    if pdf is not None:
        local_pdf = run_dir / "source.pdf"
        shutil.copyfile(pdf, local_pdf)
        local_pdf.chmod(0o600)
        emit("parsing", "Preparing the PDF with required GROBID structured parsing.")
        parsed = prepare_uploaded_pdf_source(
            local_pdf,
            run_dir,
            progress=emit,
            request_fulltext=grobid_request,
            allow_pypdf_fallback=allow_pypdf_fallback,
        )
        input_source = "uploaded_pdf"
        source_filename = pdf.name
    else:
        assert pub_id is not None
        source_aminer_key = aminer_api_key()
        parsed = download_aminer_source(
            pub_id,
            run_dir,
            api_key=source_aminer_key,
            proxy=proxy_url(),
            progress=emit,
        )
        source_manifest = (
            dict(parsed.get("manifest"))
            if isinstance(parsed.get("manifest"), dict)
            else {}
        )
        input_source = "aminer_interface_b"
        source_filename = f"aminer-{pub_id}.md"
    source_parse_path = _write_json(
        run_dir / "source_parse.json",
        {
            "input_type": "pdf" if pdf is not None else "aminer_pub_id",
            "pub_id": pub_id,
            "input_source": input_source,
            "provider": parsed.get("provider"),
            "fallback_used": bool(parsed.get("fallback_used")),
            "pypdf_fallback_authorized": (
                allow_pypdf_fallback if pdf is not None else None
            ),
            "grobid_tei_file": parsed.get("grobid_tei_file"),
            "aminer_source_manifest_file": (
                "source_manifest.json" if source_manifest else None
            ),
            "aminer_structured_fallback_used": (
                bool(source_manifest.get("structured_fallback_used"))
                if source_manifest
                else None
            ),
            "aminer_structured_fallback_reason": source_manifest.get(
                "structured_fallback_reason"
            ),
            "diagnostics": list(parsed.get("diagnostics") or []),
        },
        force=True,
    )
    pages = [_utf8_safe(page) for page in parsed.get("pages", [])]
    markdown = _utf8_safe(parsed.get("markdown") or "")
    locations = [dict(item) for item in parsed.get("source_locations", []) if isinstance(item, dict)]
    anchor_count = len(build_anchor_catalog(pages, source_locations=locations))
    diagnostic = assess_input_viability(
        pages,
        anchor_count=anchor_count,
        minimum_text_characters=800,
        minimum_anchors=4,
    )
    diagnostic.update(_structured_location_metadata(pages, locations))
    diagnostic["input_source"] = input_source
    diagnostic["parser"] = str(parsed.get("provider") or "unknown")
    if not diagnostic["reviewable"]:
        raise ValueError("The manuscript text is not sufficiently reviewable for this audit.")
    local_title = str(parsed.get("local_title") or "").strip() or _extract_local_title(pages)

    local_authors: list[str] = []
    local_year: int | None = None
    if pdf is not None:
        tei_xml = str(parsed.get("grobid_tei") or "")
        local_authors = grobid_document_authors(tei_xml)
        local_year = grobid_document_year(tei_xml)

    search_aminer_key = source_aminer_key
    if search_provider == "aminer" and search_aminer_key is None:
        search_aminer_key = aminer_api_key()
    if search_aminer_key:
        trace.secrets.append(search_aminer_key)

    identity_resolution: dict[str, Any]
    if pub_id:
        identity_resolution = {
            "provider": "AMiner Open Platform",
            "status": "input_pub_id",
            "resolved_pub_id": pub_id,
            "reason": "The manuscript source was an explicit AMiner pub_id.",
            "cost": {"currency": "CNY", "estimated_total_yuan": 0.0, "calls": 0},
        }
    elif relation_pub_id:
        identity_resolution = {
            "provider": "user_supplied",
            "status": "provided_relation_pub_id",
            "resolved_pub_id": relation_pub_id,
            "reason": "The caller supplied an explicit relation ID for this PDF.",
            "cost": {"currency": "CNY", "estimated_total_yuan": 0.0, "calls": 0},
        }
    elif search_provider == "aminer" and search_aminer_key is not None:
        identity_resolution = resolve_title_to_pub_id(
            local_title,
            authors=local_authors,
            manuscript_year=local_year,
            review_as_of=review_as_of,
            api_key=search_aminer_key,
            proxy=proxy_url(),
        )
        matched_id = identity_resolution.get("resolved_pub_id")
        if identity_resolution.get("status") == "matched" and matched_id:
            relation_pub_id = normalize_pub_id(str(matched_id))
    else:
        identity_resolution = {
            "provider": "AMiner Open Platform",
            "status": "not_attempted",
            "resolved_pub_id": None,
            "reason": (
                "Title-to-pub_id resolution is attempted automatically only "
                "for the default AMiner provider or an explicit relation ID."
            ),
            "cost": {"currency": "CNY", "estimated_total_yuan": 0.0, "calls": 0},
        }
    identity_path = _write_json(
        run_dir / "identity_resolution.json", identity_resolution, force=True
    )
    emit(
        "retrieval",
        "AMiner manuscript identity resolution is complete.",
        {
            "identity_status": identity_resolution.get("status"),
            "relation_pub_id": relation_pub_id,
        },
        {"identity_resolution": identity_path.name},
    )

    relation_context: dict[str, Any] = {}
    relation_path: Path | None = None
    if relation_pub_id:
        relation_key = source_aminer_key
        if relation_key is None:
            try:
                relation_key = aminer_api_key()
            except ValueError as exc:
                relation_context = unavailable_relation_context(
                    relation_pub_id, review_as_of, str(exc)
                )
        if relation_key is not None:
            try:
                relation_context = fetch_aminer_relation_context(
                    relation_pub_id,
                    review_as_of=review_as_of,
                    api_key=relation_key,
                    proxy=proxy_url(),
                )
            except Exception as exc:
                relation_context = unavailable_relation_context(
                    relation_pub_id,
                    review_as_of,
                    f"Unexpected AMiner relation failure: {type(exc).__name__}: {exc}",
                )
        relation_path = _write_json(
            run_dir / "relation_graph.json", relation_context, force=True
        )
        emit(
            "retrieval",
            "AMiner backward-reference expansion is ready.",
            {
                "relation_status": relation_context.get("status"),
                "external_target_reference_records": relation_context.get(
                    "summary", {}
                ).get(
                    "target_reference_ids", 0
                ),
            },
            {"relation_graph": relation_path.name},
        )

    model_call = lambda system, user, max_tokens: llm_json(
        profile,
        system,
        user,
        max_tokens=max_tokens,
        audit=trace,
    )
    # Build the local bibliography before discovery.  Besides the final
    # coverage check, this gives exact named-work searches a manuscript-local
    # relevance and cutoff signal without relaxing dates for external-only
    # candidates.
    if local_pdf is not None:
        ledger = build_reference_ledger_for_pdf(
            local_pdf,
            grobid_tei=str(parsed.get("grobid_tei") or ""),
            local_pages=[
                _utf8_safe(page) for page in parsed.get("physical_pages", [])
            ],
            request_fulltext=grobid_request,
        )
    else:
        ledger = build_reference_ledger(
            pages,
            parser="aminer_interface_b_markdown",
        )
        ledger["parser_diagnostics"] = [
            {
                "parser": "aminer_interface_b_markdown",
                "status": str(ledger.get("status") or "unknown"),
                "message": (
                    "Segmented the bibliography from AMiner Interface-B "
                    "content blocks or Markdown without a PDF reparse."
                ),
            }
        ]
    _write_json(run_dir / "reference_ledger.json", ledger, force=True)
    local_references, bibliography_ready = _retrieval_reference_inputs(ledger)
    queries = build_related_work_queries(markdown, review_as_of=review_as_of, llm_json=model_call, local_title=local_title)
    _write_json(run_dir / "retrieval_plan.json", {
        "queries": [{"query": str(q), "role": getattr(q, "role", ""),
                     "target_id": getattr(q, "target_id", ""),
                     "query_shape": getattr(q, "query_shape", ""),
                     "fallback_phrase": getattr(q, "fallback_phrase", "")} for q in queries],
        "coverage_targets": getattr(queries[0], "coverage_targets", []) if queries else [],
        "coverage_profile_diagnostics": getattr(queries[0], "coverage_profile_diagnostics", {}) if queries else {},
    }, force=True)
    emit("retrieval", "Generating queries and retrieving related-work candidates.", {"queries_generated": len(queries)}, {"queries": queries})

    def select_relevant(candidates: list[dict[str, str]], **kwargs: Any) -> tuple[list[dict[str, str]], dict[str, Any]]:
        selected, diagnostics = select_relevant_records(
            candidates, llm_json=model_call, manuscript_context=markdown, **kwargs,
            batch_size=selector_batch_size, max_workers=selector_workers,
        )
        trace({"event": "selection", "candidates": candidates, "selected": selected, "diagnostics": diagnostics})
        return selected, diagnostics

    def plan_feedback(
        candidates: list[dict[str, Any]],
        selected: list[dict[str, Any]],
        **kwargs: Any,
    ) -> tuple[list[str], dict[str, Any]]:
        return build_feedback_queries(
            candidates,
            selected,
            llm_json=model_call,
            **kwargs,
        )

    relation_controls = [
        dict(item)
        for item in relation_context.get("target_references", [])
        if isinstance(item, dict)
    ]
    neighbor_records = []
    neighbor_diagnostics: dict[str, Any] = {'status': 'disabled', 'estimated_cost_yuan': 0.0}
    if reference_seed_limit:
        emit('reference_neighbors', 'Expanding verified manuscript-cited seed papers by one backward step.', {'seed_limit': reference_seed_limit})
        neighbor_records, neighbor_diagnostics = expand_reference_neighbors(
            local_references, known_cited_controls=relation_controls,
            bibliography_ready=bibliography_ready, queries=queries, local_title=local_title,
            review_as_of=review_as_of, api_key=search_aminer_key or '', proxy=proxy_url(),
            seed_limit=reference_seed_limit, trace=trace,
        )
        trace({'event': 'reference_neighbor_expansion', 'diagnostics': neighbor_diagnostics, 'records': neighbor_records})
    if queries or relation_controls or neighbor_records:
        retrieval_arguments: dict[str, Any] = {
            "local_title": local_title,
            "review_as_of": review_as_of,
            "proxy": proxy_url(),
            "select_relevant": select_relevant,
            "plan_feedback": plan_feedback,
            "provider": search_provider,
            "aminer_api_key": search_aminer_key,
            "aminer_search_tier": aminer_search_tier,
            "aminer_detail_limit": aminer_detail_limit,
            "local_references": local_references,
            "bibliography_ready": bibliography_ready,
            "known_cited_controls": relation_controls,
            "trace": trace,
        }
        if neighbor_records:
            retrieval_arguments['seed_records'] = neighbor_records
        if semantic_preselection:
            from .candidate_pool import semantic_candidate_pool
            retrieval_arguments['preselect_candidates'] = lambda rows, **kwargs: semantic_candidate_pool(
                rows, manuscript_context=markdown, llm_json=model_call, **kwargs,
            )
        candidates, retrieval_diagnostics = retrieve_related_work(
            queries,
            **retrieval_arguments,
        )
    else:
        candidates, retrieval_diagnostics = filter_retrieval_records(
            [],
            local_title=local_title,
            review_as_of=review_as_of,
            local_references=local_references,
            bibliography_ready=bibliography_ready,
        )
    retrieval_diagnostics['reference_expansion'] = neighbor_diagnostics
    emit("retrieval", "Related-work candidate retrieval is complete.", {
        "queries_generated": len(queries),
        "related_work_records": len(candidates),
        "retrieval_status": retrieval_diagnostics.get("status"),
    }, {"retrieval_diagnostics": retrieval_diagnostics})

    audit = assess_related_work_coverage(
        candidates,
        ledger,
        relation_references=[
            dict(item)
            for item in relation_context.get("target_references", [])
            if isinstance(item, dict)
        ],
    )
    audit["publication_window"] = dict(cutoff_metadata)
    for record in audit["records"]:
        year_text = str(record.get("published") or "")[:4]
        record["publication_after_base_year"] = bool(year_text.isdigit() and int(year_text) > base_review_year)
        record["publication_window_status"] = (
            "within_explicit_one_year_grace" if record["publication_after_base_year"]
            else "within_base_year" if year_text.isdigit() else "year_unresolved"
        )
    audit.update({
        "review_as_of": review_as_of,
        "retrieval_queries": queries,
        "retrieval_diagnostics": retrieval_diagnostics,
        "retrieval_policy": (
            RETRIEVAL_POLICIES[search_provider]
            + (
                "; public AMiner backward references are discovery hints; "
                "only the input manuscript bibliography establishes citations"
                if relation_pub_id
                else "; no verified pub_id was available for relation lookup"
            )
        ),
        "identity_resolution": identity_resolution,
    })
    if relation_pub_id:
        audit["relation_graph"] = relation_context
    relation_status = str(relation_context.get("status") or "unknown")
    if relation_pub_id and relation_status in {"failed", "partial"}:
        limitations = audit.get("limitations")
        if not isinstance(limitations, list):
            limitations = []
            audit["limitations"] = limitations
        relation_limitation = (
            "Citation-graph evidence was unavailable or incomplete; inspect "
            "relation_graph diagnostics. Search-based discovery continued."
        )
        if relation_limitation not in limitations:
            limitations.append(relation_limitation)
    same_year_uncertain = int(
        retrieval_diagnostics.get("same_year_date_uncertain_records") or 0
    )
    if same_year_uncertain:
        limitations = audit.get("limitations")
        if not isinstance(limitations, list):
            limitations = []
            audit["limitations"] = limitations
        cutoff_limitation = (
            f"{same_year_uncertain} retrieved records have year-only AMiner "
            "metadata in the cutoff year; their exact dates require human "
            "verification and were not fabricated."
        )
        if cutoff_limitation not in limitations:
            limitations.append(cutoff_limitation)
    retrieval_status = str(retrieval_diagnostics.get("status") or "unknown")
    provider_errors = retrieval_diagnostics.get("provider_errors")
    provider_errors = provider_errors if isinstance(provider_errors, list) else []
    if not candidates:
        audit["status"] = "not_assessed"
        limitations = audit.get("limitations")
        if not isinstance(limitations, list):
            limitations = []
            audit["limitations"] = limitations
        if provider_errors:
            limitation = (
                "Related-work coverage was not assessed because candidate retrieval "
                "was incomplete; inspect retrieval_diagnostics.provider_errors."
            )
        elif not queries:
            limitation = (
                "Related-work coverage was not assessed because no retrieval queries "
                "were generated."
            )
        else:
            limitation = (
                "Related-work coverage was not assessed because retrieval produced "
                "no semantically relevant candidates."
            )
        if limitation not in limitations:
            limitations.append(limitation)
    elif retrieval_status != "passed" or provider_errors:
        # Partial retrieval does not undo coverage checks on valid records.
        # In particular, duplicate shortlist IDs must not be reported as an
        # empty semantic result when the report contains assessed candidates.
        if audit.get("status") == "completed":
            audit["status"] = "partial"
        limitation = (
            "Returned candidates and their coverage results were retained, but "
            "retrieval or candidate selection was incomplete; inspect "
            "retrieval_diagnostics for provider, schema, or candidate-pool errors."
        )
        if limitation not in audit.setdefault("limitations", []):
            audit["limitations"].append(limitation)
    completed = audit.get("status") == "completed"
    report = {
        "schema_version": "1.0",
        "kind": "focused_paper_audit",
        "run_id": run_id,
        "capability": "related-work",
        "capability_description": (
            "Retrieve time-bounded related work from scholarly search and an "
            "AMiner backward-reference list, then check bibliography coverage."
            if relation_pub_id
            else "Retrieve time-bounded related work and check its coverage in the bibliography."
        ),
        "status": "completed" if completed else "partial",
        "source": {
            "filename": source_filename,
            "input_type": "pdf" if pdf is not None else "aminer_pub_id",
            "pub_id": pub_id,
            "parser": str(parsed.get("provider") or "local_pypdf"),
            "provenance": {
                "input_source": input_source,
                "source_provider": str(parsed.get("provider") or "unknown"),
                "retrieval_interface": source_manifest.get("retrieval_interface"),
                "structured_content_list_used": bool(
                    source_manifest.get("content_list_file")
                ),
                "pypdf_fallback_authorized": (
                    allow_pypdf_fallback if pdf is not None else None
                ),
                "pypdf_fallback_used": (
                    bool(parsed.get("fallback_used")) if pdf is not None else None
                ),
                "source_parse_file": source_parse_path.name,
                "identity_resolution_file": identity_path.name,
                "resolved_relation_pub_id": relation_pub_id,
            },
        },
        "run_metadata": {
            **implementation,
            **cutoff_metadata,
            "audit_trace_file": trace.path.name,
            "selector_pool_limit": SELECTION_SHORTLIST_LIMIT,
            "evaluation_view_limit": EVALUATION_VIEW_LIMIT,
            "reference_seed_limit": reference_seed_limit,
            "reference_expansion_cost_yuan": neighbor_diagnostics.get('estimated_cost_yuan'),
            "selector_batch_size": selector_batch_size,
            "selector_workers": selector_workers,
            "semantic_preselection": semantic_preselection,
            "model_profile": profile.name,
            "model_requested": profile.model,
            "review_as_of": review_as_of,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "pages_or_chunks": len(pages),
            "work_dir": str(run_dir),
            "input_source": input_source,
            "aminer_pub_id": pub_id,
            "resolved_relation_pub_id": relation_pub_id,
            "identity_resolution_status": identity_resolution.get("status"),
            "identity_resolution_file": identity_path.name,
            "search_provider": search_provider,
            "aminer_search_tier": (
                aminer_search_tier if search_provider == "aminer" else None
            ),
            "aminer_detail_limit": (
                aminer_detail_limit if search_provider == "aminer" else None
            ),
            "retrieval_provider_cost": retrieval_diagnostics.get("provider_cost"),
            "source_parse_file": source_parse_path.name,
            "parser": str(parsed.get("provider") or "local_pypdf"),
            "grobid_tei_file": parsed.get("grobid_tei_file"),
            "grobid_required": pdf is not None and not allow_pypdf_fallback,
            "pypdf_fallback_authorized": (
                allow_pypdf_fallback if pdf is not None else None
            ),
            "grobid_fallback_used": (
                bool(parsed.get("fallback_used")) if pdf is not None else None
            ),
            "structured_block_count": sum(len(item.get("blocks") or []) for item in locations),
            "parse_diagnostics": list(parsed.get("diagnostics") or []),
            "source_manifest_file": (
                "source_manifest.json" if source_manifest else None
            ),
            "source_provider": (
                source_manifest.get("source_provider")
                or str(parsed.get("provider") or "unknown")
            ),
            "physical_page_count": (
                source_manifest.get("physical_page_count")
                if source_manifest
                else len(pages)
            ),
            "logical_text_chunks": source_manifest.get("logical_text_chunks"),
            "content_block_count": source_manifest.get("content_block_count", 0),
            "content_block_type_counts": source_manifest.get(
                "content_block_type_counts", {}
            ),
            "page_coordinate_count": source_manifest.get(
                "page_coordinate_count", 0
            ),
            "explicit_page_coordinate_count": source_manifest.get(
                "explicit_page_coordinate_count", 0
            ),
            "inherited_page_block_count": source_manifest.get(
                "inherited_page_block_count", 0
            ),
            "aminer_structured_fallback_used": (
                bool(source_manifest.get("structured_fallback_used"))
                if source_manifest
                else False
            ),
            "aminer_structured_fallback_reason": source_manifest.get(
                "structured_fallback_reason"
            ),
        },
        "input_diagnostic": diagnostic,
        "results": {"related_work_coverage": audit},
    }
    if relation_pub_id and relation_path is not None:
        relation_metadata = {
            "relation_pub_id": relation_pub_id,
            "relation_graph_file": relation_path.name,
            "relation_status": relation_status,
        }
        report["source"]["provenance"].update(relation_metadata)
        report["run_metadata"].update(relation_metadata)
    report_path = _write_json(run_dir / "capability-report.json", report, force=True)
    if output:
        _write_json(output, report, force=force)
    manifest = {
        "run_id": run_id,
        "mode": "standalone-related-work",
        "capability": "related-work",
        "status": report["status"],
        "report": str(report_path),
        "reference_ledger": str(run_dir / "reference_ledger.json"),
        "requested_output": str(output.expanduser().resolve()) if output else None,
        "result_statuses": {"related_work_coverage": str(audit.get("status") or "unknown")},
        "input_source": input_source,
        "pub_id": pub_id,
        "source_manifest": (
            str(run_dir / "source_manifest.json") if source_manifest else None
        ),
        "source_parse": str(source_parse_path),
        "identity_resolution": str(identity_path),
        "search_provider": search_provider,
        "work_root": str(work_root),
        **cutoff_metadata,
    }
    if relation_pub_id and relation_path is not None:
        manifest.update(
            {
                "relation_graph": str(relation_path),
                "relation_pub_id": relation_pub_id,
                "relation_status": relation_status,
            }
        )
    emit("finalizing", "Related-work audit report is ready.", {"status": report["status"]}, manifest)
    return manifest
