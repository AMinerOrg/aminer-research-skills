"""Source-availability checks for evidence-grounded paper review."""

from __future__ import annotations

import re
from typing import Any


def assess_input_viability(
    pages: list[str],
    *,
    anchor_count: int,
    minimum_text_characters: int,
    minimum_anchors: int,
) -> dict[str, Any]:
    """Classify whether extracted local text can support paper-level claims."""
    page_characters = [len(re.sub(r"\s+", "", str(page or ""))) for page in pages]
    nonempty_pages = sum(characters > 0 for characters in page_characters)
    extracted_characters = sum(page_characters)
    if not extracted_characters or not anchor_count:
        status = "parse_unreadable"
        reason = "No usable local body text or source anchors were extracted from the manuscript."
    elif extracted_characters < minimum_text_characters or anchor_count < minimum_anchors:
        status = "evidence_insufficient"
        reason = (
            "The manuscript contains some extractable text, but not enough local evidence to support a paper-level review. "
            "Use a text-based PDF, OCR export, or a complete parsed Markdown source."
        )
    else:
        status = "reviewable"
        reason = "Local text and source anchors meet the minimum evidence threshold for review generation."
    return {
        "status": status,
        "reviewable": status == "reviewable",
        "reason": reason,
        "page_count": len(pages),
        "nonempty_page_count": nonempty_pages,
        "extracted_character_count": extracted_characters,
        "source_anchor_count": anchor_count,
        "thresholds": {
            "minimum_extracted_characters": minimum_text_characters,
            "minimum_source_anchors": minimum_anchors,
        },
        "policy": (
            "Paper identity, claims, and review findings must originate from locally retained manuscript evidence. "
            "External retrieval is not used when local evidence is insufficient."
        ),
    }
