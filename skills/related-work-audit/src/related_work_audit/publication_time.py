"""Bounded publication-time evidence without inventing a day for a year/month."""

import calendar
from datetime import date
import re
from typing import Any


def review_year_end(value: str, grace_years: int = 0) -> str:
    """Normalize the product's inclusive publication-year boundary.

    A legacy YYYY-MM-DD input selects its year as well. December 31 is an
    internal comparison boundary, never a claimed publication date.
    """
    if type(grace_years) is not int or grace_years not in (0, 1):
        raise ValueError("Publication year grace must be 0 or 1.")
    raw = str(value).strip()
    if re.fullmatch(r"\d{4}", raw):
        year = int(raw)
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        year = date.fromisoformat(raw).year
    else:
        raise ValueError("Review year must use YYYY or a valid legacy YYYY-MM-DD.")
    return date(year + grace_years, 12, 31).isoformat()


def date_bounds(value: Any) -> tuple[date, date] | None:
    value = str(value or "").strip()
    try:
        if re.fullmatch(r"\d{4}", value):
            year = int(value)
            return date(year, 1, 1), date(year, 12, 31)
        if re.fullmatch(r"\d{4}-\d{2}", value):
            year, month = map(int, value.split("-"))
            return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ].*)?", value):
            day = date.fromisoformat(value[:10])
            return day, day
    except (ValueError, OverflowError):
        pass
    return None


def arxiv_month_evidence(row: dict[str, Any], source: str) -> list[dict[str, str]]:
    """Modern arXiv identifiers encode the original submission month, not a day.

    Accept only a structured identifier, arXiv DOI, or arxiv.org record URL.
    No dates are decoded from AMiner IDs, arbitrary DOIs or surrounding prose.
    """
    found = []
    for field in ("arxiv_id", "doi", "url"):
        raw = str(row.get(field) or "").strip()
        if field == "doi":
            raw = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", raw, flags=re.I)
            match = re.fullmatch(r"10\.48550/arxiv\.(\d{4}\.\d{4,5})(?:v\d+)?", raw, re.I)
        elif field == "url":
            match = re.fullmatch(r"https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?(?:\.pdf)?", raw, re.I)
        else:
            match = re.fullmatch(r"(?:arxiv:)?(\d{4}\.\d{4,5})(?:v\d+)?", raw, re.I)
        if not match:
            continue
        identifier = match.group(1)
        year, month = 2000 + int(identifier[:2]), int(identifier[2:4])
        if not 1 <= month <= 12 or (year, month) < (2007, 4):
            continue
        found.append({"source": source, "field": field, "raw_value": str(row[field]),
                      "value": f"{year:04d}-{month:02d}", "precision": "month",
                      "basis": "arxiv_original_submission_month", "arxiv_id": identifier,
                      "limitation": "Original submission month only; later versions may differ."})
    if len({item["arxiv_id"] for item in found}) > 1:
        return []
    return found[:1]


def earliest_supported_time(evidence: list[dict[str, str]]) -> str:
    values = [str(item.get("value") or "") for item in evidence]
    values = [value for value in values if date_bounds(value)]
    # Compare the guaranteed upper bound. A preprint month can establish prior
    # availability even when the venue year is later; retain its real precision.
    return min(values, key=lambda value: (date_bounds(value)[1], -len(value))) if values else ""
