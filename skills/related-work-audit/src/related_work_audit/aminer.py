"""Private AMiner Interface-B transport and manuscript normalization.

This module is intentionally self-contained: the standalone Skill does not
import the repository core, the Demo server, or another Skill.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import httpx

from .identity import aminer_headers


AMINER_EXTRACTED_FILES_URL = (
    "https://datacenter.aminer.cn/gateway/open_platform/api/v3/paper/extracted/files"
)
AMINER_PUB_ID = re.compile(r"^[0-9a-f]{24}$", re.IGNORECASE)
MAX_AMINER_MARKDOWN_BYTES = 12 * 1024 * 1024
MAX_AMINER_JSON_BYTES = 40 * 1024 * 1024
MAX_AMINER_INDEX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
Progress = Callable[[str, str, dict[str, Any] | None, dict[str, Any] | None], None]


def normalize_pub_id(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not AMINER_PUB_ID.fullmatch(normalized):
        raise ValueError(
            "AMiner pub_id must be a 24-character hexadecimal identifier."
        )
    return normalized


def _utf8_safe(value: Any) -> str:
    return str(value).encode("utf-8", errors="replace").decode("utf-8")


def markdown_semantic_chunks(
    markdown: str,
    *,
    target_chars: int = 4200,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Split page-less Markdown into bounded section-aware review units."""
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", markdown) if part.strip()]
    chunks: list[str] = []
    locations: list[dict[str, Any]] = []
    current: list[str] = []
    current_size = 0
    current_section = "Front matter"
    chunk_section = current_section
    section_block_number = 0
    block_start: int | None = None
    block_end: int | None = None

    def flush() -> None:
        nonlocal current, current_size, block_start, block_end
        if not current:
            return
        chunks.append("\n\n".join(current))
        locations.append(
            {
                "section": chunk_section,
                "paragraph_start": block_start,
                "paragraph_end": block_end,
                "block_start": block_start,
                "block_end": block_end,
                "location_unit": "markdown_block",
                "location_numbering": "section_local",
                "blocks": [],
            }
        )
        current = []
        current_size = 0
        block_start = None
        block_end = None

    for paragraph in paragraphs:
        heading = re.fullmatch(r"#{1,6}\s+(.+?)\s*#*", paragraph)
        if heading:
            flush()
            current_section = " ".join(heading.group(1).split())
            section_block_number = 0
            chunk_section = current_section
            current = [paragraph]
            current_size = len(paragraph)
            continue
        section_block_number += 1
        addition = len(paragraph) + (2 if current else 0)
        if current and block_start is not None and current_size + addition > target_chars:
            flush()
            chunk_section = current_section
        if not current:
            chunk_section = current_section
        current.append(paragraph)
        current_size += addition
        block_start = block_start or section_block_number
        block_end = section_block_number
    flush()
    if chunks:
        return chunks, locations
    if not markdown:
        return [], []
    return [markdown], [
        {
            "section": "Document",
            "paragraph_start": 1,
            "paragraph_end": 1,
            "block_start": 1,
            "block_end": 1,
            "location_unit": "markdown_block",
            "location_numbering": "section_local",
            "blocks": [],
        }
    ]


def _caption_text(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(
            " ".join(str(item).split()) for item in value if str(item).strip()
        ).strip()
    return " ".join(str(value or "").split()).strip()


def _render_content_block(row: dict[str, Any]) -> tuple[str, bool]:
    block_type = str(row.get("type") or "").lower()
    if block_type == "discarded":
        return "", False
    if block_type == "text":
        text = _utf8_safe(row.get("text") or "").strip()
        if not text:
            return "", False
        try:
            level = int(row.get("text_level"))
        except (TypeError, ValueError):
            level = 0
        if level > 0:
            return f'{"#" * max(1, min(6, level))} {text}', True
        return text, False
    if block_type == "equation":
        return _utf8_safe(row.get("text") or "").strip(), False
    if block_type == "image":
        image_path = str(row.get("img_path") or "").strip()
        caption = _caption_text(row.get("image_caption"))
        footnote = _caption_text(row.get("image_footnote"))
        parts = [f"![{caption}]({image_path})" if image_path else ""]
        parts.extend(value for value in (caption, footnote) if value)
        return "\n\n".join(part for part in parts if part), False
    if block_type == "table":
        caption = _caption_text(row.get("table_caption"))
        body = _utf8_safe(row.get("table_body") or "").strip()
        footnote = _caption_text(row.get("table_footnote"))
        image_path = str(row.get("img_path") or "").strip()
        rendered = body or (f"![{caption}]({image_path})" if image_path else "")
        return "\n\n".join(part for part in (caption, rendered, footnote) if part), False
    return _utf8_safe(row.get("text") or "").strip(), False


def content_list_context(
    content_list: list[dict[str, Any]],
    middle: dict[str, Any] | None = None,
) -> tuple[str, list[str], list[dict[str, Any]], dict[str, int]]:
    """Build Markdown and physical-page evidence units from MinerU blocks."""
    rows = [row for row in content_list if isinstance(row, dict)]
    page_indices: list[int] = []
    for row in rows:
        try:
            page_index = int(row.get("page_idx"))
        except (TypeError, ValueError):
            continue
        if page_index >= 0:
            page_indices.append(page_index)
    middle_pages = middle.get("pdf_info") if isinstance(middle, dict) else []
    middle_pages = middle_pages if isinstance(middle_pages, list) else []
    page_count = max(max(page_indices) + 1 if page_indices else 0, len(middle_pages))
    pages: list[list[str]] = [[] for _ in range(page_count)]
    page_blocks: list[list[dict[str, Any]]] = [[] for _ in range(page_count)]
    document_parts: list[str] = []
    current_section = "Front matter"
    section_block = 0
    document_block = 0
    page_block_counts = [0] * page_count
    type_counts: Counter[str] = Counter()
    current_page_index = -1

    for row in rows:
        block_type = str(row.get("type") or "unknown").lower()
        type_counts[block_type] += 1
        try:
            explicit_page_index = int(row.get("page_idx"))
        except (TypeError, ValueError):
            explicit_page_index = -1
        if 0 <= explicit_page_index < page_count:
            current_page_index = explicit_page_index
        page_index = current_page_index if 0 <= current_page_index < page_count else -1
        row["_resolved_page_idx"] = page_index if page_index >= 0 else None
        row["_page_idx_inherited"] = (
            page_index >= 0 and explicit_page_index != page_index
        )
        rendered, is_heading = _render_content_block(row)
        if not rendered:
            continue
        document_parts.append(rendered)
        if is_heading:
            current_section = re.sub(r"^#{1,6}\s+", "", rendered).strip() or current_section
            section_block = 0
        else:
            section_block += 1
            document_block += 1
        if not (0 <= page_index < page_count):
            continue
        pages[page_index].append(rendered)
        if not is_heading:
            page_block_counts[page_index] += 1
        page_blocks[page_index].append(
            {
                "type": block_type,
                "markdown": rendered,
                "is_heading": is_heading,
                "section": current_section,
                "section_block": None if is_heading else section_block,
                "page_block": None if is_heading else page_block_counts[page_index],
                "document_block": None if is_heading else document_block,
                "bbox": row.get("bbox") if isinstance(row.get("bbox"), list) else [],
                "img_path": str(row.get("img_path") or ""),
                "page_inherited": bool(row["_page_idx_inherited"]),
            }
        )

    rendered_pages: list[str] = []
    source_locations: list[dict[str, Any]] = []
    for page_index in range(page_count):
        blocks = page_blocks[page_index]
        rendered_pages.append("\n\n".join(pages[page_index]))
        sections = list(
            dict.fromkeys(
                str(block.get("section") or "Document")
                for block in blocks
                if not block.get("is_heading")
            )
        )
        middle_page = (
            middle_pages[page_index]
            if page_index < len(middle_pages)
            and isinstance(middle_pages[page_index], dict)
            else {}
        )
        source_locations.append(
            {
                "physical_page": page_index + 1,
                "page_index": page_index,
                "section": " / ".join(sections[:3]) or "Document",
                "sections": sections,
                "location_unit": "aminer_content_block",
                "location_numbering": "physical_page_and_section_block",
                "page_size": (
                    middle_page.get("page_size")
                    if isinstance(middle_page.get("page_size"), list)
                    else []
                ),
                "blocks": blocks,
            }
        )
    return (
        "\n\n".join(document_parts).strip(),
        rendered_pages,
        source_locations,
        dict(type_counts),
    )


def _trusted_download_url(value: str) -> bool:
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").rstrip(".").lower()
    return (
        parsed.scheme == "https"
        and (hostname == "aliyuncs.com" or hostname.endswith(".aliyuncs.com"))
        and not parsed.username
        and not parsed.password
    )


def _first_group(payload: Any) -> dict[str, Any]:
    groups = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(groups, list) and groups and isinstance(groups[0], dict):
        return groups[0]
    return {}


def _json_payload(response: httpx.Response, *, byte_limit: int, label: str) -> Any:
    if len(response.content) > byte_limit:
        raise ValueError(f"AMiner {label} exceeds its configured safety limit.")
    try:
        return json.loads(response.content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"AMiner {label} is not valid JSON.") from exc


def _trusted_get(
    client: httpx.Client,
    url: str,
    *,
    byte_limit: int,
) -> httpx.Response:
    """Download one artifact while validating every redirect destination."""
    current = url
    for redirect_count in range(MAX_REDIRECTS + 1):
        if not _trusted_download_url(current):
            raise ValueError(
                "AMiner returned an untrusted artifact download URL; download was refused."
            )
        try:
            response = client.get(current, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise ValueError("AMiner artifact download failed.") from exc
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("location")
            if not location or redirect_count == MAX_REDIRECTS:
                raise ValueError(
                    "AMiner artifact redirect could not be followed safely."
                )
            current = urljoin(current, location)
            continue
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ValueError(
                f"AMiner artifact download failed with HTTP {response.status_code}."
            ) from exc
        if len(response.content) > byte_limit:
            raise ValueError("AMiner artifact exceeds its configured safety limit.")
        return response
    raise ValueError("AMiner artifact redirect limit exceeded.")


def _write_private_text(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)


def _write_private_json(path: Path, value: Any) -> None:
    _write_private_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def download_aminer_source(
    pub_id: str,
    run_dir: Path,
    *,
    api_key: str,
    proxy: str | None,
    progress: Progress | None = None,
    client_factory: Callable[..., Any] = httpx.Client,
) -> dict[str, Any]:
    """Fetch and normalize one AMiner Interface-B parsed manuscript."""
    normalized_id = normalize_pub_id(pub_id)
    if not str(api_key or "").strip():
        raise ValueError("AMINER_API_KEY is empty; --pub-id cannot be fetched.")
    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_dir.chmod(0o700)
    if progress:
        progress(
            "parsing",
            "Requesting parsed manuscript files from AMiner interface B.",
            {"aminer_pub_id": normalized_id},
            None,
        )

    headers = aminer_headers(api_key)
    with client_factory(
        timeout=90.0,
        proxy=proxy,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        structure_response = client.post(
            AMINER_EXTRACTED_FILES_URL,
            headers=headers,
            json={"pub_id": normalized_id},
        )
        structure_response.raise_for_status()
        structure_group = _first_group(
            _json_payload(
                structure_response,
                byte_limit=MAX_AMINER_INDEX_BYTES,
                label="structured-file index",
            )
        )
        json_urls = (
            structure_group.get("json")
            if isinstance(structure_group.get("json"), list)
            else []
        )

        markdown_response = client.post(
            AMINER_EXTRACTED_FILES_URL,
            headers=headers,
            json={"pub_id": normalized_id, "file_types": ".md"},
        )
        markdown_response.raise_for_status()
        markdown_group = _first_group(
            _json_payload(
                markdown_response,
                byte_limit=MAX_AMINER_INDEX_BYTES,
                label="Markdown-file index",
            )
        )
        markdown_urls = (
            markdown_group.get("md")
            if isinstance(markdown_group.get("md"), list)
            else []
        )

        content_list: list[dict[str, Any]] = []
        middle: dict[str, Any] = {}
        for raw_url in json_urls:
            json_url = str(raw_url)
            response = _trusted_get(
                client,
                json_url,
                byte_limit=MAX_AMINER_JSON_BYTES,
            )
            payload = _json_payload(
                response,
                byte_limit=MAX_AMINER_JSON_BYTES,
                label="structured artifact",
            )
            path = urlsplit(json_url).path
            if path.endswith("_content_list.json"):
                raw_rows = (
                    payload
                    if isinstance(payload, list)
                    else payload.get("content_list", [])
                    if isinstance(payload, dict)
                    else []
                )
                content_list = [row for row in raw_rows if isinstance(row, dict)]
            elif path.endswith("_middle.json") and isinstance(payload, dict):
                middle = payload

        provider_markdown = ""
        if markdown_urls:
            markdown_url = str(markdown_urls[0])
            response = _trusted_get(
                client,
                markdown_url,
                byte_limit=MAX_AMINER_MARKDOWN_BYTES,
            )
            provider_markdown = _utf8_safe(
                response.content.decode("utf-8", errors="replace")
            ).strip()

    type_counts: dict[str, int] = {}
    working_content_list: list[dict[str, Any]] = []
    if content_list:
        working_content_list = [dict(row) for row in content_list]
        markdown, pages, source_locations, type_counts = content_list_context(
            working_content_list, middle
        )
    else:
        markdown, pages, source_locations = "", [], []
    source_provider = "aminer_interface_b_content_list"
    fallback_reason = None
    if not markdown:
        if not provider_markdown:
            raise ValueError(
                "AMiner interface B returned no usable manuscript content for this pub_id."
            )
        markdown = provider_markdown
        pages, source_locations = markdown_semantic_chunks(markdown)
        source_provider = "aminer_interface_b_markdown_fallback"
        fallback_reason = "structured content_list was unavailable or empty"
    elif not any(page.strip() for page in pages):
        pages, source_locations = markdown_semantic_chunks(markdown)
        source_provider = "aminer_interface_b_content_list_semantic_chunks"
        fallback_reason = "structured blocks had no usable physical-page indices"
    if not markdown or not pages:
        raise ValueError(
            "AMiner interface B returned no usable manuscript content for this pub_id."
        )
    has_physical_pages = source_provider == "aminer_interface_b_content_list"

    markdown_path = run_dir / "source.md"
    _write_private_text(markdown_path, markdown)
    provider_markdown_file = None
    if provider_markdown and provider_markdown != markdown:
        provider_path = run_dir / "source_provider.md"
        _write_private_text(provider_path, provider_markdown)
        provider_markdown_file = provider_path.name
    content_list_file = None
    if content_list:
        content_path = run_dir / "source_content_list.json"
        _write_private_json(content_path, content_list)
        content_list_file = content_path.name
    middle_file = None
    if middle:
        middle_path = run_dir / "source_middle.json"
        _write_private_json(middle_path, middle)
        middle_file = middle_path.name
    locations_path = run_dir / "source_locations.json"
    _write_private_json(locations_path, source_locations)

    manifest = {
        "input_source": "aminer_interface_b",
        "aminer_pub_id": normalized_id,
        "retrieval_interface": "AMiner interface B extracted files",
        "source_provider": source_provider,
        "markdown_file": markdown_path.name,
        "provider_markdown_file": provider_markdown_file,
        "content_list_file": content_list_file,
        "middle_file": middle_file,
        "source_locations_file": locations_path.name,
        "markdown_bytes": len(markdown.encode("utf-8")),
        "physical_page_count": len(pages) if has_physical_pages else None,
        "logical_text_chunks": None if has_physical_pages else len(pages),
        "content_block_count": len(content_list),
        "content_block_type_counts": type_counts,
        "page_coordinate_count": sum(
            1
            for row in working_content_list
            if isinstance(row.get("_resolved_page_idx"), int)
            and isinstance(row.get("bbox"), list)
        ),
        "explicit_page_coordinate_count": sum(
            1
            for row in working_content_list
            if isinstance(row.get("page_idx"), int)
            and isinstance(row.get("bbox"), list)
        ),
        "inherited_page_block_count": sum(
            bool(row.get("_page_idx_inherited")) for row in working_content_list
        ),
        "structured_fallback_used": fallback_reason is not None,
        "structured_fallback_reason": fallback_reason,
        "asset_policy": (
            "Image binaries are not downloaded because this focused related-work "
            "audit uses manuscript text and bibliography evidence; table bodies, "
            "captions, equations, image captions, page indices, and bounding boxes "
            "remain in the normalized source context."
        ),
        "location_unit": (
            "aminer_content_block" if has_physical_pages else "markdown_block"
        ),
        "location_numbering": (
            "physical_page_and_section_block" if has_physical_pages else "section_local"
        ),
    }
    manifest_path = run_dir / "source_manifest.json"
    _write_private_json(manifest_path, manifest)
    if progress:
        progress(
            "parsing",
            "AMiner interface B manuscript structure is ready.",
            {
                "pages_or_chunks": len(pages),
                "content_blocks": len(content_list),
                "source_provider": source_provider,
                "structured_fallback_used": fallback_reason is not None,
            },
            {"source_manifest": manifest_path.name},
        )
    return {
        "markdown": markdown,
        "pages": pages,
        "source_locations": source_locations,
        "provider": source_provider,
        "manifest": manifest,
        "diagnostics": [
            {
                "parser": source_provider,
                "status": "fallback" if fallback_reason else "completed",
                "message": (
                    f"AMiner produced {len(content_list)} structured content blocks "
                    f"across {len(pages)} pages or logical chunks."
                    + (f" Fallback: {fallback_reason}." if fallback_reason else "")
                ),
            }
        ],
        "fallback_used": fallback_reason is not None,
    }


__all__ = [
    "AMINER_EXTRACTED_FILES_URL",
    "content_list_context",
    "download_aminer_source",
    "markdown_semantic_chunks",
    "normalize_pub_id",
]
