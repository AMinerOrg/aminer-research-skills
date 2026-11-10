"""Evidence-anchor construction used by the manuscript input gate."""

from __future__ import annotations

import re
from typing import Any

def sentence_bounded_spans(text: str, *, target_words: int) -> list[str]:
    """Group complete sentence/line units without cutting through words."""
    normalized = text.strip()
    if not normalized:
        return []
    units = [
        unit.strip()
        for unit in re.split(r'(?<=[.!?。！？])\s*(?=(?:[A-Z0-9$\[(]|[\u4e00-\u9fff]))|\n+', normalized)
        if unit.strip()
    ]
    if not units:
        return [normalized]
    if len(units) == 1 and len(units[0].split()) > target_words:
        words = units[0].split()
        return [' '.join(words[start:start + target_words]) for start in range(0, len(words), target_words)]
    spans: list[str] = []
    current: list[str] = []
    current_words = 0
    for unit in units:
        unit_words = len(unit.split())
        if current and current_words + unit_words > target_words:
            spans.append(' '.join(current))
            current = []
            current_words = 0
        current.append(unit)
        current_words += unit_words
    if current:
        spans.append(' '.join(current))
    return spans


def build_anchor_catalog(
    pages: list[str],
    *,
    source_locations: list[dict[str, Any]] | None = None,
    chunk_words: int = 115,
    max_chunks_per_page: int = 5,
) -> dict[str, dict[str, Any]]:
    """Create model-selectable, verbatim evidence spans across the manuscript.

    The model chooses an ID instead of reproducing a long quote and guessing its
    page. The server then materializes the exact source span before validation.
    """
    catalog: dict[str, dict[str, Any]] = {}
    for page_number, page_text in enumerate(pages, start=1):
        source = (
            source_locations[page_number - 1]
            if source_locations and page_number <= len(source_locations)
            else None
        )
        structured_blocks = source.get('blocks') if isinstance(source, dict) and isinstance(source.get('blocks'), list) else []
        raw_blocks = [part.strip() for part in re.split(r'\n\s*\n', page_text) if part.strip()]
        content_blocks = [block for block in raw_blocks if not re.fullmatch(r'#{1,6}\s+(.+?)\s*#*', block)]
        if not content_blocks:
            continue
        chunks: list[tuple[str, dict[str, Any] | None]] = []
        if structured_blocks:
            block_inputs = [
                (str(block.get('markdown') or '').strip(), block)
                for block in structured_blocks
                if isinstance(block, dict) and not block.get('is_heading') and str(block.get('markdown') or '').strip()
            ]
        else:
            first_block = source.get('block_start', source.get('paragraph_start')) if source else None
            block_inputs = [
                (block, {'section_block': first_block + offset} if isinstance(first_block, int) else None)
                for offset, block in enumerate(content_blocks)
            ]
        for block, block_metadata in block_inputs:
            for span in sentence_bounded_spans(block, target_words=chunk_words):
                if len(span.split()) >= 6 or len(span) >= 24:
                    chunks.append((span, block_metadata))
        # PDF pages have no stable block identity, so retain the historical
        # per-page cap. Markdown chunks preserve at least one exact anchor for
        # every source block and are already bounded upstream.
        if not source and len(chunks) > max_chunks_per_page:
            indexes = sorted({round(index * (len(chunks) - 1) / (max_chunks_per_page - 1)) for index in range(max_chunks_per_page)})
            chunks = [chunks[index] for index in indexes]
        for chunk_number, (quote, block_metadata) in enumerate(chunks, start=1):
            anchor_id = f'P{page_number}S{chunk_number}'
            entry: dict[str, Any] = {
                'page': page_number,
                'quote': quote,
                'anchor_version': 'sentence_block_v2',
            }
            if source:
                block_metadata = block_metadata or {}
                block_number = block_metadata.get('section_block')
                section = str(block_metadata.get('section') or source.get('section') or 'Document')
                structured_location = bool(structured_blocks)
                source_unit = str(source.get('location_unit') or 'aminer_content_block')
                structured_location_type = (
                    'grobid_tei_block'
                    if source_unit == 'grobid_tei_block'
                    else 'aminer_content_list_block'
                )
                entry.update({
                    'location_type': structured_location_type if structured_location else 'markdown_section_paragraph',
                    'section': section,
                    'paragraph_start': block_number,
                    'paragraph_end': block_number,
                    'block_start': block_number,
                    'block_end': block_number,
                    'physical_page': page_number if structured_location else None,
                    'page_index': page_number - 1 if structured_location else None,
                    'page_block': block_metadata.get('page_block'),
                    'document_block': block_metadata.get('document_block'),
                    'source_block_type': block_metadata.get('type'),
                    'bbox': block_metadata.get('bbox') or [],
                    'location_unit': source_unit if structured_location else 'markdown_block',
                    'location_numbering': (
                        str(source.get('location_numbering') or 'physical_page_and_section_block')
                        if structured_location else 'section_local'
                    ),
                    'location_label': (
                        f'Page {page_number}, {section}, content block {block_number}'
                        if structured_location
                        else f'{section}, content block {block_number}' if block_number else section
                    ),
                })
            catalog[anchor_id] = entry
    return catalog
