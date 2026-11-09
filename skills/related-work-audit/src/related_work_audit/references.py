"""Local, auditable extraction of bibliography entries from PDF text or GROBID TEI.

This deliberately keeps reference segmentation separate from external scholarly
search. A downstream resolver must not be asked to judge records that could
not be cleanly recovered from the uploaded manuscript.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any
from xml.etree import ElementTree as etree


MARKDOWN_HEADING_PREFIX = r"(?:#{1,6}\s*)?"
MARKDOWN_HEADING_SUFFIX = r"(?:\s*#*)?"
REFERENCE_HEADING = re.compile(
    rf"^\s*{MARKDOWN_HEADING_PREFIX}(?:\d+\s+)?(?:references|bibliography){MARKDOWN_HEADING_SUFFIX}\s*$",
    re.IGNORECASE,
)
APPENDIX_HEADING = re.compile(
    rf"^\s*{MARKDOWN_HEADING_PREFIX}(?:[A-Z]\s+)?appendix(?:\s+[A-Z0-9].*)?{MARKDOWN_HEADING_SUFFIX}\s*$",
    re.IGNORECASE,
)
# Conference papers often start appendices with a bare lettered heading such as
# "A Evaluation details".  Once a bibliography has started, that heading closes
# the primary reference list; later embedded reports may contain their own
# References section and must not be treated as manuscript bibliography.
LETTERED_APPENDIX_HEADING = re.compile(r"^\s*[A-Z]\s+[A-Z][A-Za-z0-9][^.!?]{2,120}\s*$")
PAGE_NUMBER = re.compile(r"^\s*\d{1,3}\s*$")
URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
DOI = re.compile(r"\b10\.\d{4,9}/[-._;()/:a-z0-9]+", re.IGNORECASE)
ARXIV = re.compile(r"\b(?:arxiv\s*:\s*|arxiv\.org/(?:abs|pdf)/)(\d{4}\.\d{4,5}(?:v\d+)?)", re.IGNORECASE)
YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
ATTACHED_REVIEW_LINE_NUMBER = re.compile(r"(?<=[A-Za-z)\].,;:])\d{3}$")

# Common author-first bibliography styles. This intentionally rejects prose
# continuations such as "In Proceedings" and "URL ...".
AUTHOR_START = re.compile(
    r"^\s*(?:\[\d{1,3}\]\s*)?"
    r"(?:[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'`´-]*(?:\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'`´-]*){1,5}|"
    r"[A-Z]{1,4}\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'`´-]*)\s*(?:,|\.)"
)
TWO_AUTHOR_START = re.compile(
    r"^\s*(?:\[\d{1,3}\]\s*)?"
    r"[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'`´-]*(?:\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'`´-]*){1,5}"
    r"\s+and\s+"
    r"[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'`´-]*(?:\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'`´-]*){1,5}\s*\."
)
NON_TITLE_PREFIX = re.compile(
    r"^(?:in\b|proceedings\b|pp\.?\b|pages?\b|volume\b|vol\.?\b|url\b|doi\b|arxiv\b|isbn\b)",
    re.IGNORECASE,
)
PUBLISHER_OR_URL_TAIL = re.compile(
    r"^(?:url\s+https?://|openreview\.net\b|pmlr\b|curran\s+associates\b|"
    r"proceedings\.mlr\.press\b|proceedings\.neurips\.cc\b|papers\.nips\.cc\b)",
    re.IGNORECASE,
)


def _clean_line(line: str) -> str:
    value = " ".join(str(line or "").split())
    # ACL review PDFs append a three-digit source line number directly to text
    # (for example, ``References732``). Remove only attached counters so page
    # ranges and years separated by whitespace remain intact.
    value = ATTACHED_REVIEW_LINE_NUMBER.sub("", value)
    # pypdf often keeps a line-break hyphen as "inter- national".
    return re.sub(r"(?<=[A-Za-z])-\s+(?=[a-z])", "", value)


def _is_page_header(line: str) -> bool:
    lowered = line.lower()
    return (
        PAGE_NUMBER.fullmatch(line) is not None
        or lowered.startswith("published as a conference paper at ")
        or lowered.startswith("under review as a conference paper at ")
    )


def _is_reference_start(line: str) -> bool:
    if not (AUTHOR_START.match(line) or TWO_AUTHOR_START.match(line)):
        return False
    prefix = line[:80].lower()
    return not prefix.startswith(("in ", "url ", "doi ", "arxiv ", "proceedings "))


def _join_lines(lines: list[str]) -> str:
    value = " ".join(line for line in lines if line)
    return _clean_line(value)


def _candidate_title(raw: str) -> str | None:
    """Conservatively select the first title-like sentence after author text."""
    pieces = re.split(r"(?<=[a-z0-9])\.\s+(?=[A-Z])", raw)
    for index, piece in enumerate(pieces):
        candidate = piece.strip(" .")
        letters = len(re.findall(r"[A-Za-z]", candidate))
        if letters < 8 or URL.search(candidate) or NON_TITLE_PREFIX.match(candidate):
            continue
        # The first sentence in a reference is normally the author list.
        if index == 0 and (candidate.count(",") >= 1 or len(candidate.split()) <= 6):
            continue
        # Very long comma-heavy strings are almost always merged author lists.
        if candidate.count(",") >= 5 or len(candidate) > 360:
            continue
        candidate = re.split(
            r"\.\s*(?=(?:arxiv\b|in\b|proceedings\b|url\b|doi\b))",
            candidate,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip(" .")
        return candidate
    return None


def _reference_record(reference_id: int, pages: list[int], lines: list[str]) -> dict[str, Any]:
    raw = _join_lines(lines)
    title = _candidate_title(raw)
    dois = list(dict.fromkeys(match.rstrip(".,;)") for match in DOI.findall(raw)))
    arxiv_ids = list(dict.fromkeys(match.lower() for match in ARXIV.findall(raw)))
    urls = list(dict.fromkeys(match.rstrip(".,;)") for match in URL.findall(raw)))
    years = [int(value) for value in YEAR.findall(raw)]
    issues: list[str] = []
    if len(raw) < 45:
        issues.append("record_too_short")
    if len(raw) > 1800:
        issues.append("record_unusually_long")
    if not title and not dois and not arxiv_ids:
        issues.append("no_title_or_persistent_identifier")
    if raw.count("http") > 3 or raw.count("arXiv:") > 3:
        issues.append("possible_multiple_references_merged")
    confidence = "high"
    if issues:
        confidence = "low" if any(issue in issues for issue in ("record_too_short", "record_unusually_long", "possible_multiple_references_merged")) else "medium"
    return {
        "id": f"R{reference_id}",
        "raw": raw,
        "candidate_title": title,
        "doi": dois[0] if dois else None,
        "arxiv_id": arxiv_ids[0] if arxiv_ids else None,
        "urls": urls[:3],
        "year_candidates": years,
        "anchor": {"page_start": min(pages), "page_end": max(pages), "quote": raw[:600]},
        "parse_confidence": confidence,
        "parse_issues": issues,
    }


def _annotate_resolution_eligibility(records: list[dict[str, Any]]) -> None:
    """Keep parser fragments visible without treating them as standalone citations.

    These annotations are deliberately conservative. They do not merge or delete
    raw records; they only stop obvious, identity-free tail fragments from being
    sent to an external metadata resolver as if they were separate references.
    """
    for index, record in enumerate(records):
        record['resolution_eligible'] = True
        record['parser_disposition'] = 'standalone_candidate'
        record['parser_link'] = None
        raw = str(record.get('raw') or '').strip()
        has_identity = bool(record.get('candidate_title') or record.get('doi') or record.get('arxiv_id'))
        previous = records[index - 1] if index else None
        previous_has_identity = bool(
            previous and (previous.get('candidate_title') or previous.get('doi') or previous.get('arxiv_id'))
        )
        if not has_identity and previous_has_identity and PUBLISHER_OR_URL_TAIL.match(raw):
            record['resolution_eligible'] = False
            record['parser_disposition'] = 'linked_publisher_or_url_fragment'
            record['parser_link'] = {
                'action': 'linked_to_previous_for_parse_context',
                'target_id': previous['id'],
                'reason': 'Identity-free publisher or URL tail immediately follows a structured reference.',
            }
        elif not has_identity and len(raw) <= 16:
            record['resolution_eligible'] = False
            record['parser_disposition'] = 'isolated_short_fragment'
            record['parser_link'] = {
                'action': 'excluded_from_external_resolution',
                'target_id': None,
                'reason': 'Very short identity-free parser residue; retained for parser audit only.',
            }


def _build_ledger(records: list[dict[str, Any]], *, heading_found: bool, parser: str) -> dict[str, Any]:
    """Attach common quality gates to records from a declared parser."""
    _annotate_resolution_eligibility(records)
    resolver_records = [record for record in records if record['resolution_eligible']]
    issue_counts = Counter(issue for record in resolver_records for issue in record["parse_issues"])
    low_confidence = sum(record["parse_confidence"] == "low" for record in resolver_records)
    title_or_identifier = sum(
        bool(record["candidate_title"] or record["doi"] or record["arxiv_id"])
        for record in resolver_records
    )
    total = len(resolver_records)
    quality_ratio = title_or_identifier / total if total else 0.0
    malformed_ratio = low_confidence / total if total else 1.0
    status = "ready_for_resolution"
    reasons: list[str] = []
    if not heading_found:
        status = "references_not_found"
        reasons.append("No References or Bibliography heading was found by the parser.")
    elif total < 3:
        status = "parse_quality_insufficient"
        reasons.append("Fewer than three bibliography entries could be segmented from the PDF.")
    elif malformed_ratio > 0.15:
        status = "parse_quality_insufficient"
        reasons.append(f"{low_confidence}/{total} entries have structural parse warnings.")
    elif quality_ratio < 0.65:
        status = "parse_quality_insufficient"
        reasons.append(f"Only {title_or_identifier}/{total} entries expose a candidate title or persistent identifier.")

    return {
        "status": status,
        "parser": parser,
        "records": records,
        "summary": {
            "references_heading_found": heading_found,
            "records_segmented": len(records),
            "records_eligible_for_resolution": total,
            "records_with_title_or_identifier": title_or_identifier,
            "low_confidence_records": low_confidence,
            "linked_fragment_records": sum(
                record['parser_disposition'] == 'linked_publisher_or_url_fragment'
                for record in records
            ),
            "isolated_short_fragment_records": sum(
                record['parser_disposition'] == 'isolated_short_fragment'
                for record in records
            ),
            "issue_counts": dict(sorted(issue_counts.items())),
            "parse_quality_ratio": round(quality_ratio, 3),
            "malformed_ratio": round(malformed_ratio, 3),
            "gate_reasons": reasons,
        },
    }


def build_reference_ledger(pages: list[str], *, parser: str = "local_pypdf") -> dict[str, Any]:
    """Build a bibliography ledger from page text supplied by a declared parser."""
    started = False
    stopped = False
    block_delimited = parser == "aminer_interface_b_markdown"
    current_lines: list[str] = []
    current_pages: list[int] = []
    records: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal current_lines, current_pages
        if current_lines and current_pages:
            records.append(_reference_record(len(records) + 1, current_pages, current_lines))
        current_lines, current_pages = [], []

    for page_number, page_text in enumerate(pages, start=1):
        for raw_line in str(page_text or "").splitlines():
            line = _clean_line(raw_line)
            if not line:
                # AMiner content-list blocks are joined with blank lines before
                # reaching this parser. In the bibliography, that boundary is
                # more reliable than author-name heuristics (for example,
                # "Sho Takase and Shun Kiyono.").
                if started and block_delimited and current_lines:
                    flush()
                continue
            if not started:
                if REFERENCE_HEADING.fullmatch(line):
                    started = True
                continue
            # Initial-plus-surname author lines (e.g. "M Pawan Kumar, ...")
            # resemble bare lettered headings. They must not truncate the
            # bibliography and hide all later citations from coverage checks.
            bare_appendix = (
                LETTERED_APPENDIX_HEADING.fullmatch(line)
                and not _is_reference_start(line)
                and not re.search(r"[,;()]", line)
                and (not current_lines or YEAR.search(_join_lines(current_lines)))
            )
            if APPENDIX_HEADING.fullmatch(line) or bare_appendix:
                flush()
                stopped = True
                break
            if _is_page_header(line):
                continue
            if _is_reference_start(line) or (block_delimited and not current_lines):
                flush()
                current_lines = [line]
                current_pages = [page_number]
            elif current_lines:
                current_lines.append(line)
                current_pages.append(page_number)
        if stopped:
            break
    flush()

    return _build_ledger(records, heading_found=started, parser=parser)


def _node_text(node: etree.Element | None) -> str:
    return _clean_line(" ".join(node.itertext())) if node is not None else ""


def _grobid_pages(node: etree.Element) -> list[int]:
    pages: list[int] = []
    for value in node.attrib.get("coords", "").split(";"):
        page, _, _ = value.partition(",")
        if page.isdigit():
            pages.append(int(page))
    return pages or [1]


def _grobid_reference_record(reference_id: int, node: etree.Element) -> dict[str, Any]:
    title_node = node.find(".//{*}analytic/{*}title[@type='main']")
    if title_node is None:
        title_node = node.find(".//{*}analytic/{*}title")
    if title_node is None:
        title_node = node.find(".//{*}monogr/{*}title[@level='m']")
    title = _node_text(title_node) or None
    raw_node = node.find(".//{*}note[@type='raw_reference']")
    raw = _node_text(raw_node) or _node_text(node)
    doi = None
    arxiv_id = None
    urls: list[str] = []
    for identifier in node.findall(".//{*}idno"):
        value = _node_text(identifier).strip().rstrip(".,;)")
        kind = identifier.attrib.get("type", "").lower()
        if kind == "doi" or DOI.fullmatch(value):
            doi = doi or value
        elif kind in {"arxiv", "arxivid"}:
            arxiv_id = arxiv_id or value.removeprefix("arXiv:").strip()
        elif value.startswith(("http://", "https://")):
            urls.append(value)
    urls.extend(URL.findall(raw))
    urls = list(dict.fromkeys(value.rstrip(".,;)") for value in urls))[:3]
    years = [int(value) for value in YEAR.findall(raw)]
    pages = _grobid_pages(node)
    issues: list[str] = []
    if len(raw) < 25:
        issues.append("record_too_short")
    if not title and not doi and not arxiv_id:
        issues.append("no_title_or_persistent_identifier")
    confidence = "high" if title else ("medium" if doi or arxiv_id else "low")
    authors = [_node_text(author) for author in node.findall(".//{*}author/{*}persName")]
    date_node = node.find(".//{*}imprint/{*}date[@type='published']")
    date_value = date_node.attrib.get('when', '') if date_node is not None else ''
    publication_year = int(date_value[:4]) if re.match(r'^\d{4}', date_value) else None
    return {
        "id": f"R{reference_id}",
        "raw": raw,
        "candidate_title": title,
        "doi": doi,
        "arxiv_id": arxiv_id,
        "urls": urls,
        "year_candidates": years,
        "authors": authors,
        "publication_year": publication_year,
        "anchor": {"page_start": min(pages), "page_end": max(pages), "quote": raw[:600]},
        "parse_confidence": confidence,
        "parse_issues": issues,
        "parser_source": "grobid",
        "parser_reference_id": node.attrib.get("{http://www.w3.org/XML/1998/namespace}id"),
        "parser_coords_available": bool(node.attrib.get("coords", "").strip()),
    }


def build_reference_ledger_from_grobid(tei_xml: str) -> dict[str, Any]:
    """Build a ledger from GROBID TEI bibliography records without inventing fields."""
    root = etree.fromstring(tei_xml)
    bibliography = root.find(".//{*}listBibl")
    nodes = bibliography.findall("{*}biblStruct") if bibliography is not None else []
    records = [_grobid_reference_record(index, node) for index, node in enumerate(nodes, start=1)]
    return _build_ledger(records, heading_found=bibliography is not None, parser="grobid_tei")


def restrict_grobid_ledger_to_primary_bibliography(
    grobid_ledger: dict[str, Any],
    local_ledger: dict[str, Any],
) -> dict[str, Any]:
    """Restrict GROBID records to the PDF's first, primary bibliography span.

    GROBID may merge a second reference list from an embedded appendix artifact
    into the document-level ``listBibl``.  The local page parser is less useful
    for field extraction but preserves section order, so a healthy local ledger
    supplies the authoritative start/end pages while GROBID remains responsible
    for structured bibliography fields.
    """
    grobid_records = list(grobid_ledger.get("records") or [])
    local_records = list(local_ledger.get("records") or [])
    if local_ledger.get("status") != "ready_for_resolution" or len(local_records) < 3:
        return grobid_ledger
    if not grobid_records or not all(record.get("parser_coords_available") for record in grobid_records):
        return grobid_ledger

    first_page = min(int(record["anchor"]["page_start"]) for record in local_records)
    last_page = max(int(record["anchor"]["page_end"]) for record in local_records)
    retained = [
        record for record in grobid_records
        if int(record.get("anchor", {}).get("page_end") or 0) >= first_page
        and int(record.get("anchor", {}).get("page_start") or 10**9) <= last_page
    ]
    if len(retained) < 3 or len(retained) == len(grobid_records):
        return grobid_ledger

    excluded = [record for record in grobid_records if record not in retained]
    scoped = _build_ledger(retained, heading_found=True, parser="grobid_tei")
    scoped["scope_filter"] = {
        "status": "applied",
        "strategy": "first_primary_bibliography_page_span",
        "page_start": first_page,
        "page_end": last_page,
        "records_before": len(grobid_records),
        "records_after": len(retained),
        "excluded_records": len(excluded),
        "excluded_parser_reference_ids": [
            record.get("parser_reference_id") for record in excluded
            if record.get("parser_reference_id")
        ],
    }
    return scoped
