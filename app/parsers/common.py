"""Primitives shared by every parser.

LinkedIn expresses the same concepts differently across its API generations --
`timePeriod` vs `dateRange`, `picture` vs `profilePicture.displayImageReference`,
`companyName` vs `company.name`. Rather than duplicate a parser per generation,
these helpers accept every spelling that has been observed, so a shape change
on LinkedIn's side usually degrades one field instead of breaking a section.

Every function here is total: it returns a valid object or None, and never
raises on malformed input. That is deliberate -- a single unexpected null deep
in a 400KB payload must not cost the caller the whole profile.
"""

from __future__ import annotations

import re
from datetime import UTC
from typing import Any

from ..models import DateParts, DateRange, ImageAsset, Organization

_WS = re.compile(r"[ \t ]+")
_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


# ---------------------------------------------------------------------------
# Access helpers
# ---------------------------------------------------------------------------


def pick(obj: Any, *keys: str, default: Any = None) -> Any:
    """First present, non-empty value among `keys`. Supports 'a.b.c' paths."""
    if not isinstance(obj, dict):
        return default
    for key in keys:
        node: Any = obj
        for part in key.split("."):
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(part)
        if node not in (None, "", [], {}):
            return node
    return default


def text(value: Any) -> str | None:
    """Normalise a text value. LinkedIn wraps many strings in {text: ...}."""
    if isinstance(value, dict):
        value = pick(value, "text", "value", "name", "localized")
    if not isinstance(value, str):
        return None
    cleaned = _WS.sub(" ", value).strip()
    # Collapse runs of 3+ newlines but keep paragraph breaks.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned or None


def as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        digits = re.sub(r"[^\d]", "", value)
        if digits:
            return int(digits)
    return None


def urn_id(urn: Any) -> str | None:
    """'urn:li:fs_profile:ACoAAA' -> 'ACoAAA'."""
    if isinstance(urn, str) and ":" in urn:
        return urn.rsplit(":", 1)[-1] or None
    return None


# ---------------------------------------------------------------------------
# Dates
#
# LinkedIn only ever stores month precision, and often just a year. Both API
# generations use {year, month, day} objects; the wrapper key differs.
# ---------------------------------------------------------------------------


def parse_date(node: Any) -> DateParts | None:
    if not isinstance(node, dict):
        return None
    year = as_int(node.get("year"))
    month = as_int(node.get("month"))
    day = as_int(node.get("day"))
    if year is None and month is None and day is None:
        return None
    return DateParts(year=year, month=month, day=day, text=_date_text(year, month))


def _date_text(year: int | None, month: int | None) -> str | None:
    if year and month and 1 <= month <= 12:
        return f"{_MONTHS[month - 1][:3]} {year}"
    if year:
        return str(year)
    return None


def parse_date_range(node: Any, *, duration: bool = True) -> DateRange:
    """Handles both `timePeriod` (legacy) and `dateRange` (dash) shapes.

    `duration=False` for ranges that are not a tenure -- a certificate's
    issue-to-expiry window is not "3 yrs 1 mo of experience".
    """
    if not isinstance(node, dict):
        return DateRange()
    period = node
    for wrapper in ("timePeriod", "dateRange"):
        if isinstance(node.get(wrapper), dict):
            period = node[wrapper]
            break

    start = parse_date(pick(period, "startDate", "start"))
    end = parse_date(pick(period, "endDate", "end"))
    is_current = end is None and start is not None
    return DateRange(
        start=start,
        end=end,
        is_current=is_current,
        duration_text=_duration_text(start, end) if duration else None,
    )


def _duration_text(start: DateParts | None, end: DateParts | None) -> str | None:
    """LinkedIn shows '3 yrs 2 mos'. profileView omits it, so derive it.

    Only computed when both endpoints have a year, and only when an end date
    exists -- 'to now' would need a clock and would make cached responses drift.
    """
    if not start or not start.year or not end or not end.year:
        return None
    if start.month is None or end.month is None:
        # Year-only range: claiming month precision we never had would be a lie.
        years = end.year - start.year
        return f"{years} yr{'s' if years != 1 else ''}" if years > 0 else None
    months = (end.year - start.year) * 12 + (end.month - start.month) + 1
    if months <= 0:
        return None
    years, rem = divmod(months, 12)
    parts = []
    if years:
        parts.append(f"{years} yr{'s' if years != 1 else ''}")
    if rem:
        parts.append(f"{rem} mo{'s' if rem != 1 else ''}")
    return " ".join(parts) or None


# ---------------------------------------------------------------------------
# Images
#
# LinkedIn returns a VectorImage: one signed rootUrl plus a list of size
# artifacts whose paths are appended to it. Callers get every size, because
# which one you want depends on what you are building.
# ---------------------------------------------------------------------------


def parse_vector_image(node: Any) -> list[ImageAsset]:
    if not isinstance(node, dict):
        return []

    # Unwrap the layers each generation adds around the same object. Note the
    # literal-key pass must come first: LinkedIn's $type keys contain dots, so
    # pick()'s "a.b.c" path syntax would shred "com.linkedin.common.VectorImage".
    for literal in ("com.linkedin.common.VectorImage", "vectorImage", "artifactImage"):
        candidate = node.get(literal)
        if isinstance(candidate, dict):
            node = candidate
            break
    else:
        for path in (
            "displayImageReference.vectorImage",
            "displayImageReferenceResolutionResult.vectorImage",
            "image.com.linkedin.common.VectorImage",
        ):
            candidate = pick(node, path)
            if isinstance(candidate, dict):
                node = candidate
                break

    root = node.get("rootUrl")
    artifacts = node.get("artifacts")
    if not isinstance(root, str) or not isinstance(artifacts, list):
        # Some payloads carry a plain, already-absolute url.
        direct = pick(node, "url", "displayImageUrl")
        return [ImageAsset(url=direct)] if isinstance(direct, str) else []

    assets: list[ImageAsset] = []
    for art in artifacts:
        if not isinstance(art, dict):
            continue
        segment = pick(art, "fileIdentifyingUrlPathSegment", "path")
        if not isinstance(segment, str):
            continue
        expires = as_int(art.get("expiresAt"))
        assets.append(
            ImageAsset(
                url=root + segment,
                width=as_int(art.get("width")),
                height=as_int(art.get("height")),
                # LinkedIn sends epoch milliseconds.
                expires_at=_epoch_ms(expires),
            )
        )
    assets.sort(key=lambda a: (a.width or 0))
    return assets


def _epoch_ms(value: int | None):
    if not value:
        return None
    from datetime import datetime

    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Organisations
# ---------------------------------------------------------------------------


def parse_organization(node: Any, *, fallback_name: Any = None) -> Organization | None:
    """A company / school / issuing authority, from any of its shapes."""
    if isinstance(node, str):
        # A dangling URN: the entity was withheld, but the identity is real.
        if node.startswith("urn:"):
            return Organization(urn=node)
        return Organization(name=text(node))

    if not isinstance(node, dict):
        name = text(fallback_name)
        return Organization(name=name) if name else None

    inner = pick(node, "miniCompany", "miniSchool", "company", "school")
    if isinstance(inner, dict):
        outer = {k: v for k, v in node.items() if k not in ("miniCompany", "miniSchool")}
        node = {**inner, **outer}

    name = text(
        pick(node, "name", "companyName", "schoolName", "localizedName")
    ) or text(fallback_name)
    urn = pick(node, "entityUrn", "objectUrn", "companyUrn", "schoolUrn")
    universal = pick(node, "universalName", "companyUniversalName")

    url = None
    if isinstance(universal, str):
        kind = "school" if "School" in str(pick(node, "$type", default="")) else "company"
        url = f"https://www.linkedin.com/{kind}/{universal}"
    elif isinstance(urn, str) and ":company:" in urn:
        url = f"https://www.linkedin.com/company/{urn_id(urn)}"

    logo = parse_vector_image(pick(node, "logo", "logoResolutionResult", "image"))

    if not any((name, urn, logo)):
        return None
    return Organization(
        name=name,
        urn=urn if isinstance(urn, str) else None,
        linkedin_url=url,
        logo=logo,
    )


def compact(items: list[Any]) -> list[Any]:
    """Drop entries that ended up carrying no information at all."""
    out = []
    for item in items:
        if item is None:
            continue
        dumped = item.model_dump(exclude_none=True) if hasattr(item, "model_dump") else item
        if isinstance(dumped, dict):
            meaningful = {k: v for k, v in dumped.items() if v not in (None, "", [], {}, False)}
            if not meaningful:
                continue
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Enums
#
# LinkedIn returns SCREAMING_SNAKE constants in several places where the UI
# shows a sentence. Callers should not have to keep their own lookup table, so
# map the known ones and fall back to a readable transform for the rest.
# ---------------------------------------------------------------------------

_PROFICIENCY = {
    "NATIVE_OR_BILINGUAL": "Native or bilingual proficiency",
    "FULL_PROFESSIONAL": "Full professional proficiency",
    "PROFESSIONAL_WORKING": "Professional working proficiency",
    "LIMITED_WORKING": "Limited working proficiency",
    "ELEMENTARY": "Elementary proficiency",
}

_PRONOUNS = {
    "SHE_HER": "she/her",
    "HE_HIM": "he/him",
    "THEY_THEM": "they/them",
}


def humanize(value: Any, table: dict[str, str] | None = None) -> str | None:
    """Turn an enum constant into the string LinkedIn actually displays."""
    raw = text(value)
    if raw is None:
        return None
    if table and raw in table:
        return table[raw]
    # SCREAMING_SNAKE and single-word SCREAMING both occur: dash returns
    # "SCIENCE_AND_TECHNOLOGY" and plain "EDUCATION" for the same field.
    # Require more than two characters so country codes ("IN", "US") and other
    # legitimate all-caps short values are left alone.
    if len(raw) > 2 and raw.isupper() and raw.replace("_", "").isalpha():
        return raw.replace("_", " ").capitalize()
    return raw


def humanize_proficiency(value: Any) -> str | None:
    return humanize(value, _PROFICIENCY)


def humanize_pronouns(value: Any) -> str | None:
    return humanize(value, _PRONOUNS)
