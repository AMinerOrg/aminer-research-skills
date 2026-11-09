from __future__ import annotations

import re
import xml.etree.ElementTree as etree
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from pypdf import PdfReader


@dataclass
class MarkdownParseResult:
    markdown: str
    pages: list[str]
    content_list: list[dict[str, Any]] | None = None
    provider: str = 'local_pypdf'
    source_locations: list[dict[str, Any]] | None = None


TEI_NAMESPACE = 'http://www.tei-c.org/ns/1.0'
TEI = f'{{{TEI_NAMESPACE}}}'


def _tei_text(node: etree.Element | None) -> str:
    if node is None:
        return ''
    return ' '.join(''.join(node.itertext()).split())


def _coordinate_pages(node: etree.Element) -> list[int]:
    pages: list[int] = []
    for candidate in node.iter():
        coords = str(candidate.get('coords') or '')
        for box in coords.split(';'):
            token = box.split(',', 1)[0].strip()
            if not token:
                continue
            try:
                page = int(float(token))
            except ValueError:
                continue
            if page > 0 and page not in pages:
                pages.append(page)
    return pages


def _normalized_alignment_text(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', str(value or '').casefold())


def _align_text_to_page(text: str, physical_pages: list[str], default_page: int) -> int:
    needle = _normalized_alignment_text(text)
    if len(needle) >= 32:
        probes = (needle[:160], needle[-160:])
        normalized_pages = [_normalized_alignment_text(page) for page in physical_pages]
        for probe in probes:
            if len(probe) < 32:
                continue
            for index, page in enumerate(normalized_pages, start=1):
                if probe in page:
                    return index
    return max(1, min(default_page, len(physical_pages) or 1))


def parse_grobid_fulltext(
    tei_xml: str,
    *,
    physical_pages: list[str],
) -> MarkdownParseResult:
    """Build physical-page, section-aware review text from GROBID TEI.

    Sentence coordinates determine physical pages. The pypdf page text is used
    only to align TEI blocks without coordinates and to retain the document page
    count; callers should fall back to ``parse_pdf_locally`` if TEI parsing fails.
    """
    root = etree.fromstring(tei_xml)
    body = root.find(f'.//{TEI}text/{TEI}body')
    if body is None:
        raise ValueError('GROBID TEI does not contain a document body.')

    coordinate_pages = _coordinate_pages(body)
    page_count = max([len(physical_pages), *coordinate_pages, 1])
    page_parts: list[list[str]] = [[] for _ in range(page_count)]
    page_blocks: list[list[dict[str, Any]]] = [[] for _ in range(page_count)]
    content_list: list[dict[str, Any]] = []
    page_sections: list[set[str]] = [set() for _ in range(page_count)]
    section_counts: dict[str, int] = {}
    document_block = 0
    last_page = 1

    def add_block(
        text: str,
        *,
        page: int,
        section: str,
        block_type: str,
        coords: str = '',
    ) -> None:
        nonlocal document_block, last_page
        rendered = ' '.join(str(text or '').split())
        if not rendered:
            return
        page = max(1, min(page, page_count))
        page_index = page - 1
        section = section.strip() or 'Document'
        if section not in page_sections[page_index]:
            page_sections[page_index].add(section)
            page_parts[page_index].append(f'## {section}')
            page_blocks[page_index].append({
                'type': 'heading',
                'markdown': f'## {section}',
                'is_heading': True,
                'section': section,
                'section_block': None,
                'page_block': None,
                'document_block': None,
                'coords': '',
                'bbox': [],
            })
        section_counts[section] = section_counts.get(section, 0) + 1
        document_block += 1
        page_block = sum(not item.get('is_heading') for item in page_blocks[page_index]) + 1
        metadata = {
            'type': block_type,
            'markdown': rendered,
            'is_heading': False,
            'section': section,
            'section_block': section_counts[section],
            'page_block': page_block,
            'document_block': document_block,
            'coords': coords,
            'bbox': [],
        }
        page_parts[page_index].append(rendered)
        page_blocks[page_index].append(metadata)
        content_list.append({
            'page_idx': page_index,
            'type': block_type,
            'text': rendered,
            'section': section,
            'coords': coords,
        })
        last_page = page

    title = _tei_text(root.find(f'.//{TEI}teiHeader//{TEI}titleStmt/{TEI}title'))
    if title:
        page_parts[0].append(title)
        page_blocks[0].append({
            'type': 'title', 'markdown': title, 'is_heading': True,
            'section': 'Front matter', 'section_block': None,
            'page_block': None, 'document_block': None, 'coords': '', 'bbox': [],
        })
    abstract = _tei_text(root.find(f'.//{TEI}teiHeader//{TEI}abstract'))
    if abstract:
        add_block(abstract, page=1, section='Abstract', block_type='abstract')

    def paragraph_blocks(node: etree.Element, section: str) -> None:
        nonlocal last_page
        sentences = node.findall(f'.//{TEI}s')
        if not sentences:
            text = _tei_text(node)
            pages = _coordinate_pages(node)
            page = pages[0] if pages else _align_text_to_page(text, physical_pages, last_page)
            add_block(
                text,
                page=page,
                section=section,
                block_type='text',
                coords=str(node.get('coords') or ''),
            )
            return
        groups: list[tuple[int, list[str], list[str]]] = []
        for sentence in sentences:
            text = _tei_text(sentence)
            if not text:
                continue
            pages = _coordinate_pages(sentence)
            page = pages[0] if pages else _align_text_to_page(text, physical_pages, last_page)
            coords = str(sentence.get('coords') or '')
            if groups and groups[-1][0] == page:
                groups[-1][1].append(text)
                if coords:
                    groups[-1][2].append(coords)
            else:
                groups.append((page, [text], [coords] if coords else []))
        for page, texts, coords in groups:
            add_block(
                ' '.join(texts),
                page=page,
                section=section,
                block_type='text',
                coords=';'.join(coords),
            )

    def walk_div(node: etree.Element, inherited_section: str) -> None:
        head = node.find(f'./{TEI}head')
        section = _tei_text(head) or inherited_section or 'Document'
        for child in list(node):
            local = child.tag.rsplit('}', 1)[-1]
            if local == 'head':
                continue
            if local == 'div':
                walk_div(child, section)
            elif local == 'p':
                paragraph_blocks(child, section)
            elif local in {'formula', 'figure', 'table'}:
                text = _tei_text(child)
                pages = _coordinate_pages(child)
                page = pages[0] if pages else _align_text_to_page(text, physical_pages, last_page)
                block_type = 'table' if child.get('type') == 'table' else local
                add_block(
                    text,
                    page=page,
                    section=section,
                    block_type=block_type,
                    coords=str(child.get('coords') or ''),
                )
            elif local == 'list':
                for item in child.findall(f'./{TEI}item'):
                    text = _tei_text(item)
                    pages = _coordinate_pages(item)
                    page = (
                        pages[0]
                        if pages
                        else _align_text_to_page(text, physical_pages, last_page)
                    )
                    add_block(
                        text,
                        page=page,
                        section=section,
                        block_type='list_item',
                        coords=str(item.get('coords') or ''),
                    )

    for child in list(body):
        local = child.tag.rsplit('}', 1)[-1]
        if local == 'div':
            walk_div(child, 'Document')
        elif local in {'formula', 'figure', 'table'}:
            text = _tei_text(child)
            pages = _coordinate_pages(child)
            page = pages[0] if pages else _align_text_to_page(text, physical_pages, last_page)
            block_type = 'table' if child.get('type') == 'table' else local
            section = (
                'Tables'
                if block_type == 'table'
                else 'Figures' if block_type == 'figure' else 'Document'
            )
            add_block(
                text,
                page=page,
                section=section,
                block_type=block_type,
                coords=str(child.get('coords') or ''),
            )

    if document_block == 0:
        raise ValueError('GROBID TEI body did not contain reviewable text blocks.')

    rendered_pages = ['\n\n'.join(parts).strip() for parts in page_parts]
    source_locations: list[dict[str, Any]] = []
    for page_index, blocks in enumerate(page_blocks):
        sections = list(dict.fromkeys(
            str(block.get('section') or 'Document')
            for block in blocks
            if not block.get('is_heading')
        ))
        source_locations.append({
            'physical_page': page_index + 1,
            'page_index': page_index,
            'section': ' / '.join(sections[:3]) or 'Document',
            'sections': sections,
            'location_unit': 'grobid_tei_block',
            'location_numbering': 'physical_page_and_section_block',
            'blocks': blocks,
        })
    markdown = '\n\n'.join(
        f'## Page {index}\n\n{page}'
        for index, page in enumerate(rendered_pages, start=1)
        if page
    ).strip()
    return MarkdownParseResult(
        markdown=markdown,
        pages=rendered_pages,
        content_list=content_list,
        provider='local_grobid_tei_hybrid',
        source_locations=source_locations,
    )


def parse_pdf_locally(pdf_bytes: bytes) -> MarkdownParseResult:
    reader = PdfReader(BytesIO(pdf_bytes))
    pages: list[str] = []
    for page in reader.pages:
        text = (page.extract_text() or '').strip()
        pages.append(text)

    markdown_lines: list[str] = []
    for index, page_text in enumerate(pages, start=1):
        markdown_lines.append(f'## Page {index}')
        markdown_lines.append('')
        markdown_lines.append(page_text)
        markdown_lines.append('')

    content_list: list[dict[str, Any]] = []
    for page_idx, page_text in enumerate(pages):
        for line in page_text.splitlines():
            normalized = line.strip()
            if normalized:
                content_list.append({'page_idx': page_idx, 'type': 'text', 'text': normalized})

    markdown = '\n'.join(markdown_lines).strip()
    return MarkdownParseResult(markdown=markdown, pages=pages, content_list=content_list)
