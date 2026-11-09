"""GROBID-first bibliography ledger construction with visible pypdf fallback."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
from xml.etree import ElementTree as etree

import httpx

from .grobid import grobid_body_citation_contexts
from .parsing import parse_pdf_locally
from .references import (
    build_reference_ledger,
    build_reference_ledger_from_grobid,
    restrict_grobid_ledger_to_primary_bibliography,
)


def build_reference_ledger_for_pdf(
    pdf_path: Path,
    *,
    grobid_tei: str | None,
    local_pages: list[str] | None,
    request_fulltext: Callable[[Path], str],
) -> dict[str, Any]:
    """Reuse the body parser's TEI and fall back visibly to the local ledger."""
    diagnostics: list[dict[str, str]] = []
    try:
        pages = local_pages
        if pages is None:
            pages = parse_pdf_locally(pdf_path.read_bytes()).pages
        local_ledger = build_reference_ledger([_utf8_safe(page) for page in pages])
    except Exception:
        local_ledger = build_reference_ledger([])

    try:
        tei_xml = request_fulltext(pdf_path) if grobid_tei is None else grobid_tei
        if not tei_xml.strip():
            raise ValueError("Precomputed GROBID TEI is unavailable.")
        raw_grobid = build_reference_ledger_from_grobid(tei_xml)
        # The local ledger only supplies the primary bibliography page span.
        # GROBID remains the record parser, so this is alignment rather than a
        # pypdf fallback and is safe in strict mode.
        ledger = restrict_grobid_ledger_to_primary_bibliography(
            raw_grobid, local_ledger
        )
        scope = ledger.get("scope_filter") if isinstance(ledger.get("scope_filter"), dict) else {}
        diagnostics.append({
            "parser": "grobid_tei",
            "status": ledger["status"],
            "message": (
                f"GROBID segmented {scope.get('records_before')} entries and retained "
                f"{scope.get('records_after')} in the primary bibliography."
                if scope.get("status") == "applied"
                else f"GROBID segmented {ledger['summary']['records_segmented']} bibliography entries."
            ),
        })
        if ledger["status"] == "ready_for_resolution":
            ledger["citation_contexts"] = grobid_body_citation_contexts(tei_xml, ledger["records"])
            ledger["parser_diagnostics"] = diagnostics
            return ledger
        diagnostics.append({
            "parser": "grobid_tei",
            "status": "fallback_requested",
            "message": "GROBID output did not pass the local resolution-quality gate; trying the local parser.",
        })
    except (httpx.HTTPError, etree.ParseError, ValueError) as exc:
        diagnostics.append({
            "parser": "grobid_tei",
            "status": "unavailable",
            "message": f"GROBID could not produce a usable TEI bibliography ({type(exc).__name__}).",
        })
    ledger = local_ledger
    diagnostics.append({
        "parser": "local_pypdf",
        "status": ledger["status"],
        "message": f"Local parser segmented {ledger['summary']['records_segmented']} bibliography entries.",
    })
    ledger["parser_diagnostics"] = diagnostics
    ledger["fallback_from"] = "grobid_tei"
    return ledger


def _utf8_safe(value: Any) -> str:
    return str(value).encode("utf-8", errors="replace").decode("utf-8")
