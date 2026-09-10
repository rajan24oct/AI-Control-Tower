"""Detecting personal data in reported prompt and completion text.

The point is not to catch everything -- pattern matching cannot -- but to make
the common, unambiguous identifiers visible on the monitor, so that "this agent
is being handed card numbers" is something an operator notices rather than
something a review discovers months later.

Two things follow from that, and both matter more than detection coverage:

  * A clean scan means "nothing matched", never "no personal data present".
    Nothing in the interface may imply otherwise.
  * Highlighting must never be able to inject markup. Text arrives from
    external engines, so every span is escaped before a mark is put round it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe


@dataclass(frozen=True)
class PIIKind:
    key: str
    label: str
    pattern: re.Pattern
    #: Optional second opinion, for patterns loose enough to need one.
    validator: object = None


def _luhn(digits: str) -> bool:
    """Card-number checksum. Cuts most of what the digit pattern over-matches."""
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _is_card(match: re.Match) -> bool:
    digits = re.sub(r"\D", "", match.group())
    return 13 <= len(digits) <= 19 and _luhn(digits)


def _is_ip(match: re.Match) -> bool:
    return all(0 <= int(octet) <= 255 for octet in match.group().split("."))


#: Order matters: the first kind to claim a span keeps it, so the more specific
#: patterns are listed before the ones that would swallow them.
KINDS: tuple[PIIKind, ...] = (
    PIIKind("email", "Email address", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]*\w\b")),
    PIIKind(
        "card",
        "Payment card number",
        re.compile(r"\b\d(?:[ -]?\d){12,18}\b"),
        _is_card,
    ),
    PIIKind(
        "iban",
        "Bank account (IBAN)",
        re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){3,7}\b"),
    ),
    PIIKind(
        "ni",
        "National Insurance number",
        re.compile(r"\b[A-CEGHJ-PR-TW-Z]{2}[ ]?\d{2}[ ]?\d{2}[ ]?\d{2}[ ]?[A-D]\b"),
    ),
    PIIKind("ssn", "Social Security number", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    PIIKind(
        "postcode",
        "Postcode",
        re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?[ ]?\d[A-Z]{2}\b"),
    ),
    PIIKind(
        "phone",
        "Telephone number",
        re.compile(r"(?:\+44[ ]?|\b0)\d{2,4}[ -]?\d{3,4}[ -]?\d{3,4}\b"),
    ),
    PIIKind("ip", "IP address", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), _is_ip),
)

KINDS_BY_KEY = {kind.key: kind for kind in KINDS}


@dataclass(frozen=True)
class Finding:
    kind: str
    label: str
    start: int
    end: int


def scan(text: str) -> list[Finding]:
    """Find identifiers in `text`, resolving overlaps in favour of the earlier kind."""
    if not text:
        return []

    findings: list[Finding] = []
    claimed: list[tuple[int, int]] = []

    for kind in KINDS:
        for match in kind.pattern.finditer(text):
            if kind.validator and not kind.validator(match):
                continue
            start, end = match.span()
            if any(start < taken_end and end > taken_start for taken_start, taken_end in claimed):
                continue
            claimed.append((start, end))
            findings.append(Finding(kind.key, kind.label, start, end))

    findings.sort(key=lambda finding: finding.start)
    return findings


def summarise(findings: list[Finding]) -> list[dict]:
    """`[{key, label, count}]`, ordered as KINDS is, for a badge or a legend."""
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.kind] = counts.get(finding.kind, 0) + 1
    return [
        {"key": kind.key, "label": kind.label, "count": counts[kind.key]}
        for kind in KINDS
        if kind.key in counts
    ]


def highlight(text: str, findings: list[Finding] | None = None):
    """Escape `text` and wrap each finding in a mark. Returns safe HTML.

    Escaping happens span by span rather than up front, because escaping first
    would shift every offset the scan produced.
    """
    if not text:
        return mark_safe("")

    findings = scan(text) if findings is None else findings
    if not findings:
        return escape(text)

    parts = []
    cursor = 0
    for finding in findings:
        parts.append(escape(text[cursor:finding.start]))
        parts.append(
            format_html(
                '<mark class="ct-pii" data-pii="{}" title="{}">{}</mark>',
                finding.kind,
                finding.label,
                text[finding.start:finding.end],
            )
        )
        cursor = finding.end
    parts.append(escape(text[cursor:]))
    return mark_safe("".join(parts))
