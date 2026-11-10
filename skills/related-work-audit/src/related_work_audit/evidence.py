"""Verify source excerpts and profile labels without fuzzy semantic acceptance."""
from __future__ import annotations

import re
import unicodedata
from typing import Any


def canonical_profile(raw: Any) -> dict[str, list[str]]:
    """Use one ordered profile representation for query and target anchors."""
    raw = raw if isinstance(raw, dict) else {}
    result = {}
    for field in ("domain_terms", "task_terms", "mechanism_terms", "data_protocol_terms"):
        values = raw.get(field)
        result[field] = list(dict.fromkeys(" ".join(v.split()) for v in values
            if isinstance(v, str) and v.strip()))[:8] if isinstance(values, list) else []
    return result


def profile_anchors(profile: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Assign stable, field-specific identifiers to the supplied profile."""
    return {
        f"{prefix}{index}": {"field": field, "text": str(phrase)}
        for field, prefix in (("domain_terms", "D"), ("task_terms", "T"),
                              ("mechanism_terms", "M"), ("data_protocol_terms", "E"))
        for index, phrase in enumerate(profile.get(field, []), 1)
    }


def profile_anchor(anchor_id: Any, profile: dict[str, Any], fields: tuple[str, ...]) -> dict[str, str] | None:
    key = str(anchor_id or "").strip().upper()
    anchor = profile_anchors(profile).get(key)
    return {"id": key, **anchor} if anchor and anchor["field"] in fields else None


def normalize(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def verified_quote(quote: Any, source: str) -> bool:
    """Accept literal source text or explicitly elided, ordered source fragments."""
    text, supplied = normalize(quote), normalize(source)
    if len(text) < 24:
        return False
    if text in supplied:
        return True
    fragments = re.split(r"\s*(?:\.{3,}|…)\s*", text)
    if len(fragments) < 2 or any(len(part) < 24 for part in fragments):
        return False
    cursor = 0
    for part in fragments:
        index = supplied.find(part, cursor)
        if index < 0:
            return False
        cursor = index + len(part)
    return True


def profile_label(label: Any, profile: dict[str, Any], fields: tuple[str, ...]) -> str | None:
    """Allow qualifiers around a complete profile phrase, never drop its words."""
    tokens = lambda v: re.findall(r"\w+", normalize(v))
    stated = tokens(label)
    def same_word(left: str, right: str) -> bool:
        # Regular singular/plural forms only; no fuzzy synonyms or lost qualifiers.
        return left == right or (min(len(left), len(right)) >= 4 and (
            left + "s" == right or right + "s" == left
        ))
    for field in fields:
        for phrase in profile.get(field, []):
            canonical = tokens(phrase)
            if len(canonical) >= 2 and any(
                all(same_word(a, b) for a, b in zip(canonical, stated[i:i+len(canonical)]))
                for i in range(len(stated)-len(canonical)+1)
            ):
                return str(phrase)
    return None
