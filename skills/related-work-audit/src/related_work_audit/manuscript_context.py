"""Keep manuscript sections in bounded model context without rewriting evidence."""
from __future__ import annotations

import re


def _category(heading: str) -> str:
    text = heading.casefold()
    if re.search(r"related\s+work|prior\s+work|literature|background", text):
        return "related"
    if re.search(r"abstract|introduction|overview", text):
        return "opening"
    if re.search(r"method|approach|framework|architecture|design", text):
        return "method"
    if re.search(r"experiment|evaluat|benchmark|dataset|result|discussion|conclusion", text):
        return "evaluation"
    if re.search(r"reference|bibliograph", text):
        return "references"
    return "other"


def manuscript_context(markdown: str, max_chars: int) -> str:
    """Reserve space for late Related Work and method/evaluation sections.

    Content remains source text; section boundaries and omitted gaps are explicit.
    A bibliography is supplied separately and does not consume this context budget.
    """
    if len(markdown) <= max_chars:
        return markdown
    if max_chars <= 0:
        return ""
    headings = list(re.finditer(r"(?m)^(#{1,6})\s+([^\n]+)\n", markdown))
    groups: dict[str, list[tuple[int, int]]] = {k: [] for k in ("opening", "related", "method", "evaluation", "other")}
    parents: list[tuple[int, str]] = []
    active = "opening"
    start = 0

    def append_span(end: int) -> None:
        if end <= start or active == "references":
            return
        spans = groups[active]
        if spans and spans[-1][1] == start:
            spans[-1] = (spans[-1][0], end)
        else:
            spans.append((start, end))

    for match in headings:
        append_span(match.start())
        level, heading = len(match.group(1)), match.group(2).strip()
        # PDF page markers and figure subheads do not change the enclosing section.
        if not re.fullmatch(r"Page\s+\d+|Figures?|Tables?", heading, re.I):
            while parents and parents[-1][0] >= level:
                parents.pop()
            category = _category(heading)
            # A descriptive subsection such as "Confidence bins" belongs to
            # its Evaluation parent; it must not consume only the other budget.
            active = parents[-1][1] if category == "other" and parents else category
            parents.append((level, active))
        start = match.start()
    append_span(len(markdown))
    separator = "\n\n[... source excerpt omitted ...]\n\n"
    texts = {
        k: separator.join(markdown[a:b].strip() for a, b in spans)
        for k, spans in groups.items() if spans
    }
    if not texts:
        return markdown[:max_chars]
    # Each nonempty category receives space; unused allocations flow to others.
    weights = {"opening": 30, "related": 30, "method": 20, "evaluation": 15, "other": 5}
    available = max(0, max_chars - len(separator) * (len(texts)-1))
    total_weight = sum(weights[k] for k in texts)
    budget = {k: min(len(v), available*weights[k]//total_weight) for k,v in texts.items()}
    remaining = available - sum(budget.values())
    for k in ("related", "opening", "method", "evaluation", "other"):
        if k in texts and remaining:
            extra = min(remaining, len(texts[k])-budget[k])
            budget[k] += extra
            remaining -= extra
    result = separator.join(texts[k][:budget[k]] for k in texts if budget[k])
    return result[:max_chars]
