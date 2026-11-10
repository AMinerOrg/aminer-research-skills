"""GROBID transport and TEI parsing used by manuscript and citation workflows."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any, Callable
from xml.etree import ElementTree as etree

import httpx
from .parsing import parse_grobid_fulltext, parse_pdf_locally


ProgressCallback = Callable[
    [str, str, dict[str, Any] | None, dict[str, Any] | None],
    None,
]


def grobid_document_title(tei_xml: str) -> str:
    """Read the manuscript title from the TEI header without model inference."""
    try:
        root = etree.fromstring(tei_xml)
    except etree.ParseError:
        return ""
    title = root.find(".//{*}teiHeader/{*}fileDesc/{*}titleStmt/{*}title")
    if title is None:
        return ""
    return re.sub(r"\s+", " ", "".join(title.itertext())).strip(" .")


def grobid_document_authors(tei_xml: str) -> list[str]:
    """Read header authors for safe AMiner title disambiguation."""
    try:
        root = etree.fromstring(tei_xml)
    except etree.ParseError:
        return []
    names: list[str] = []
    for author in root.findall(
        ".//{*}teiHeader/{*}fileDesc/{*}sourceDesc//{*}analytic/{*}author"
    ) + root.findall(
        ".//{*}teiHeader/{*}fileDesc/{*}titleStmt/{*}author"
    ):
        person = author.find("{*}persName")
        node = person if person is not None else author
        name = re.sub(r"\s+", " ", " ".join(node.itertext())).strip(" ,.;")
        if name and name not in names:
            names.append(name)
    return names


def grobid_document_year(tei_xml: str) -> int | None:
    """Read an explicit header publication year without inferring one."""
    try:
        root = etree.fromstring(tei_xml)
    except etree.ParseError:
        return None
    header = root.find(".//{*}teiHeader")
    if header is None:
        return None
    for node in header.findall(".//{*}date"):
        raw = str(node.get("when") or " ".join(node.itertext()) or "")
        match = re.search(r"\b(19|20)\d{2}\b", raw)
        if match:
            return int(match.group(0))
    return None


def grobid_body_citation_contexts(
    tei_xml: str,
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Extract sentence-level bibliography contexts while preserving TEI links."""
    try:
        root = etree.fromstring(tei_xml)
    except etree.ParseError:
        return []
    reference_ids = {
        str(record.get("parser_reference_id")): str(record.get("id"))
        for record in records
        if record.get("parser_reference_id") and record.get("id")
    }
    bibliography_ids = {
        str(node.attrib.get("{http://www.w3.org/XML/1998/namespace}id"))
        for node in root.findall(".//{*}listBibl/{*}biblStruct")
        if node.attrib.get("{http://www.w3.org/XML/1998/namespace}id")
    }
    body = root.find(".//{*}text/{*}body")
    if body is None or not reference_ids:
        return []

    def paragraph_with_refs(node: etree.Element) -> tuple[str, list[dict[str, Any]]]:
        parts: list[str] = []
        references: list[dict[str, Any]] = []

        def append(value: str | None) -> None:
            if value:
                parts.append(value)

        def walk(element: etree.Element) -> None:
            append(element.text)
            for child in element:
                start = len("".join(parts))
                walk(child)
                end = len("".join(parts))
                if child.tag.endswith("ref") and child.attrib.get("type") == "bibr":
                    target_ids = [
                        target.lstrip("#")
                        for target in child.attrib.get("target", "").split()
                    ]
                    mapped = [
                        reference_ids[target]
                        for target in target_ids
                        if target in reference_ids
                    ]
                    excluded_scope_targets = [
                        target
                        for target in target_ids
                        if target in bibliography_ids and target not in reference_ids
                    ]
                    unknown_targets = [
                        target for target in target_ids if target not in bibliography_ids
                    ]
                    if (
                        target_ids
                        and not mapped
                        and excluded_scope_targets
                        and not unknown_targets
                    ):
                        append(child.tail)
                        continue
                    references.append(
                        {
                            "start": start,
                            "end": end,
                            "marker": "".join(child.itertext()).strip(),
                            "reference_ids": mapped,
                            "unmatched": not mapped
                            and (not target_ids or bool(unknown_targets)),
                        }
                    )
                append(child.tail)

        walk(node)
        return "".join(parts), references

    contexts: list[dict[str, Any]] = []
    paragraph_number = 0
    for division in body.findall(".//{*}div"):
        head = division.find("{*}head")
        section = " ".join(head.itertext()).strip() if head is not None else None
        for paragraph in division.findall("{*}p"):
            raw_text, references = paragraph_with_refs(paragraph)
            if not raw_text.strip() or not references:
                continue
            paragraph_number += 1
            boundaries = [0]
            boundaries.extend(
                match.end()
                for match in re.finditer(
                    r"(?<=[.!?])\s+(?=[A-Z0-9•])",
                    raw_text,
                )
            )
            boundaries.append(len(raw_text))
            for reference in references:
                sentence_start = max(
                    boundary for boundary in boundaries if boundary <= reference["start"]
                )
                sentence_end = next(
                    boundary for boundary in boundaries if boundary > reference["start"]
                )
                sentence = " ".join(raw_text[sentence_start:sentence_end].split())
                if len(sentence) < 24:
                    continue
                key = (section or "", paragraph_number, sentence)
                existing = next(
                    (item for item in contexts if item["_key"] == key),
                    None,
                )
                if existing is None:
                    existing = {
                        "_key": key,
                        "candidate_id": f"GCF{len(contexts) + 1}",
                        "page": None,
                        "section": section,
                        "paragraph_index": paragraph_number,
                        "citation_sentence": sentence,
                        "citation_markers": [],
                        "reference_ids": [],
                        "citation_links": [],
                        "source": "grobid_tei_body",
                    }
                    contexts.append(existing)
                if (
                    reference["marker"]
                    and reference["marker"] not in existing["citation_markers"]
                ):
                    existing["citation_markers"].append(reference["marker"])
                link = {
                    "marker": reference["marker"],
                    "reference_ids": list(reference["reference_ids"]),
                    "unmatched": bool(reference.get("unmatched")),
                    "start": max(0, reference["start"] - sentence_start),
                    "end": max(0, reference["end"] - sentence_start),
                }
                if link not in existing["citation_links"]:
                    existing["citation_links"].append(link)
                if reference.get("unmatched"):
                    existing.setdefault("unmatched_citation_markers", []).append(
                        reference["marker"] or "unmapped bibliographic ref"
                    )
                for reference_id in reference["reference_ids"]:
                    if reference_id not in existing["reference_ids"]:
                        existing["reference_ids"].append(reference_id)
    for context in contexts:
        context.pop("_key", None)

    def marker_style(marker: str) -> str:
        value = " ".join(str(marker or "").split())
        if re.search(r"[A-Za-z][^)]{0,100}\b(?:19|20)\d{2}[a-z]?\b", value):
            return "author_year"
        if re.fullmatch(r"\s*\[\s*\d+(?:\s*[-,;]\s*\d+)*\s*\]\s*", value):
            return "numeric"
        return "other"

    matched_styles = Counter(
        marker_style(link.get("marker", ""))
        for context in contexts
        for link in context.get("citation_links", [])
        if link.get("reference_ids")
    )
    dominant_style = None
    if matched_styles["author_year"] > matched_styles["numeric"]:
        dominant_style = "author_year"
    elif matched_styles["numeric"] > matched_styles["author_year"]:
        dominant_style = "numeric"

    cleaned_contexts: list[dict[str, Any]] = []
    for context in contexts:
        links = []
        for link in context.get("citation_links", []):
            if link.get("reference_ids"):
                links.append(link)
                continue
            if dominant_style and marker_style(link.get("marker", "")) != dominant_style:
                continue
            links.append(link)
        if not links:
            continue
        context["citation_links"] = links
        context["citation_markers"] = list(
            dict.fromkeys(link["marker"] for link in links if link.get("marker"))
        )
        context["reference_ids"] = list(
            dict.fromkeys(
                reference_id
                for link in links
                for reference_id in link.get("reference_ids", [])
            )
        )
        unmatched = [
            link["marker"]
            for link in links
            if link.get("unmatched") and link.get("marker")
        ]
        if unmatched:
            context["unmatched_citation_markers"] = list(dict.fromkeys(unmatched))
        else:
            context.pop("unmatched_citation_markers", None)
        context["candidate_id"] = f"GCF{len(cleaned_contexts) + 1}"
        cleaned_contexts.append(context)
    return cleaned_contexts


def request_grobid_fulltext(
    pdf_path: Path,
    grobid_url: str,
    *,
    post: Callable[..., Any] | None = None,
) -> str:
    """Submit a PDF to GROBID and return a usable TEI document."""
    request = post
    client = None
    if request is None:
        client = httpx.Client(
            timeout=httpx.Timeout(120.0, connect=5.0),
            trust_env=False,
        )
        request = client.post
    try:
        response = request(
            f"{grobid_url.rstrip('/')}/api/processFulltextDocument",
            files={"input": (pdf_path.name, pdf_path.read_bytes(), "application/pdf")},
            data={
                "includeRawCitations": "1",
                "segmentSentences": "1",
                "teiCoordinates": ["s", "ref", "biblStruct", "formula", "figure"],
            },
        )
        response.raise_for_status()
    finally:
        if client is not None:
            client.close()
    if not response.text.strip() or "[BAD_INPUT_DATA]" in response.text:
        raise ValueError("GROBID returned no usable TEI document.")
    return response.text


def prepare_uploaded_pdf_source(
    pdf_path: Path,
    run_dir: Path,
    *,
    progress: ProgressCallback | None,
    request_fulltext: Callable[[Path], str],
    allow_pypdf_fallback: bool = False,
    parse_local: Callable[[bytes], Any] = parse_pdf_locally,
    parse_grobid: Callable[..., Any] = parse_grobid_fulltext,
) -> dict[str, Any]:
    """Parse an upload with GROBID; use pypdf only when explicitly authorized."""
    run_dir.mkdir(parents=True, exist_ok=True)
    diagnostics: list[dict[str, Any]] = []
    local = None
    try:
        local = parse_local(pdf_path.read_bytes())
    except Exception as exc:
        diagnostics.append(
            {
                "parser": "local_pypdf",
                "status": "unavailable",
                "message": (
                    "pypdf page extraction was unavailable "
                    f"({type(exc).__name__}); GROBID will parse independently."
                ),
            }
        )
    tei_xml = ""
    try:
        if progress:
            progress(
                "parsing",
                "Parsing manuscript structure with the local GROBID service.",
                None,
                None,
            )
        tei_xml = request_fulltext(pdf_path)
        tei_path = run_dir / "grobid_fulltext.tei.xml"
        tei_path.write_text(tei_xml, encoding="utf-8")
        tei_path.chmod(0o600)
        parsed = parse_grobid(
            tei_xml,
            physical_pages=local.pages if local is not None else [],
        )
        diagnostics.append(
            {
                "parser": "local_grobid_tei_hybrid",
                "status": "completed",
                "message": (
                    f"GROBID produced {len(parsed.content_list or [])} structured body "
                    f"blocks across {len(parsed.pages)} physical pages."
                ),
            }
        )
        return {
            "markdown": parsed.markdown,
            "pages": parsed.pages,
            "local_title": grobid_document_title(tei_xml),
            "source_locations": parsed.source_locations or [],
            "provider": parsed.provider,
            "grobid_tei": tei_xml,
            "grobid_tei_file": tei_path.name,
            "diagnostics": diagnostics,
            "fallback_used": False,
            "physical_pages": local.pages if local is not None else [],
        }
    except (httpx.HTTPError, etree.ParseError, OSError, ValueError) as exc:
        if not allow_pypdf_fallback:
            raise ValueError(
                "GROBID could not produce a usable structured parse "
                f"({type(exc).__name__}). Start GROBID or rerun with "
                "--allow-pypdf-fallback to explicitly accept lower-quality text extraction."
            ) from exc
        if local is None:
            raise ValueError(
                "Neither GROBID nor pypdf could produce reviewable manuscript text."
            ) from exc
        diagnostics.append(
            {
                "parser": "local_grobid_tei_hybrid",
                "status": "fallback",
                "message": (
                    f"GROBID body parsing was unavailable ({type(exc).__name__}); "
                    "used local pypdf text."
                ),
            }
        )
        if progress:
            progress(
                "parsing",
                (
                    "GROBID was unavailable or unusable; using the explicitly "
                    "allowed lower-quality pypdf fallback."
                ),
                {"parser": "local_pypdf", "fallback_used": True},
                None,
            )
        return {
            "markdown": local.markdown,
            "pages": local.pages,
            "local_title": "",
            "source_locations": [],
            "provider": local.provider,
            "grobid_tei": tei_xml,
            "grobid_tei_file": None,
            "diagnostics": diagnostics,
            "fallback_used": True,
            "physical_pages": local.pages,
        }


__all__ = [
    "grobid_document_title",
    "grobid_document_authors",
    "grobid_document_year",
    "grobid_body_citation_contexts",
    "prepare_uploaded_pdf_source",
    "request_grobid_fulltext",
]
