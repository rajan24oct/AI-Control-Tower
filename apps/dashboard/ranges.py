"""Date range selection for the dashboard (section 7).

Parsing lives here rather than in the view so the same window can be handed to
every service that needs it, and so a malformed query string degrades to a
sensible default instead of raising.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from django.utils import timezone

#: Preset key -> (label, number of days including today).
PRESETS: dict[str, tuple[str, int]] = {
    "1": ("Today", 1),
    "7": ("Last 7 days", 7),
    "30": ("Last 30 days", 30),
    "90": ("Last 90 days", 90),
}

DEFAULT_PRESET = "30"
#: A year of daily buckets is already a wide chart; beyond that the series
#: stops being readable and the queries stop being cheap.
MAX_DAYS = 365


def _parse_date(raw: str) -> date | None:
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        return None


@dataclass(frozen=True)
class DateRange:
    """An inclusive day range, plus how it should be described in the UI."""

    start: date
    end: date
    label: str
    key: str

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def is_custom(self) -> bool:
        return self.key == "custom"

    @property
    def is_single_day(self) -> bool:
        return self.start == self.end

    def each_day(self) -> list[date]:
        """Every day in the window, so gaps render as zero rather than absent."""
        return [self.start + timedelta(days=offset) for offset in range(self.days)]

    def describe(self) -> str:
        """Suffix for metric labels, e.g. 'Executions (last 7 days)'."""
        if self.key == "1":
            return "today"
        if self.is_custom:
            return f"{self.start:%d %b} to {self.end:%d %b}"
        return self.label.lower()

    @classmethod
    def preset(cls, key: str = DEFAULT_PRESET, today: date | None = None) -> "DateRange":
        label, days = PRESETS.get(key, PRESETS[DEFAULT_PRESET])
        key = key if key in PRESETS else DEFAULT_PRESET
        today = today or timezone.now().date()
        return cls(start=today - timedelta(days=days - 1), end=today, label=label, key=key)

    @classmethod
    def from_request(cls, params, today: date | None = None) -> "DateRange":
        """Build a range from query parameters.

        Explicit `from`/`to` dates win over a preset. Anything unparseable
        falls back to the default window -- a broken filter should not break
        the dashboard.
        """
        today = today or timezone.now().date()
        start = _parse_date(params.get("from", ""))
        end = _parse_date(params.get("to", ""))

        if start or end:
            start = start or end
            end = end or today
            if start > end:
                start, end = end, start
            # Clamp rather than reject: the user still gets a usable window.
            if (end - start).days + 1 > MAX_DAYS:
                start = end - timedelta(days=MAX_DAYS - 1)
            return cls(
                start=start,
                end=end,
                label=f"{start:%d %b %Y} to {end:%d %b %Y}",
                key="custom",
            )

        return cls.preset(params.get("range", DEFAULT_PRESET), today=today)
