"""Mock GitHub Copilot adapter.

Makes the developer-tools view demonstrable with no GitHub organization. Seats
come from the subscription's options (`seats`: a list of {login, plan, ...}),
and usage is derived from a hash of login and day, so a demo shows the same
numbers every time it is synced and tests can assert on them.

Per-seat options shape the demo:
  profile: "heavy" | "regular" | "light" | "idle"   how much the seat is used
  idle_days: N                                       last used N days ago
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from integrations.base.devtools import BaseDeveloperToolAdapter, SeatRecord, SeatUsage
from integrations.registry import register_cost

#: Typical weekday premium requests per profile.
PROFILES = {"heavy": 70, "regular": 14, "light": 4, "idle": 0}
MODELS = ["claude-sonnet-4", "gpt-5", "gemini-2.5-pro", "o4-mini"]


def _unit(*parts) -> float:
    """A stable number in [0, 1) from the inputs."""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


@register_cost("mock_github_copilot")
class MockGitHubCopilotAdapter(BaseDeveloperToolAdapter):
    adapter_key = "mock_github_copilot"
    display_name = "GitHub Copilot (mock)"
    platform = "github_copilot"

    def _seat_specs(self) -> list[dict]:
        return list(self.config.option("seats", []) or [])

    def _today(self) -> date:
        override = self.config.option("today")
        return date.fromisoformat(override) if override else datetime.now(timezone.utc).date()

    def list_seats(self, account: str) -> list[SeatRecord]:
        today = self._today()
        seats = []
        for spec in self._seat_specs():
            idle_days = spec.get("idle_days")
            if idle_days is None:
                idle_days = 0 if spec.get("profile", "regular") != "idle" else 60
            last_activity = None
            if not spec.get("never_used"):
                last_activity = datetime.combine(
                    today - timedelta(days=int(idle_days)), time(10, 30), tzinfo=timezone.utc
                )
            seats.append(
                SeatRecord(
                    login=spec["login"],
                    plan=spec.get("plan", "business"),
                    assigned_on=today - timedelta(days=int(spec.get("assigned_days_ago", 180))),
                    display_name=spec.get("name", ""),
                    email=spec.get("email", ""),
                    assigning_team=spec.get("github_team", ""),
                    last_activity_at=last_activity,
                    last_activity_editor="" if last_activity is None else spec.get("editor", "vscode"),
                )
            )
        return seats

    def get_seat_usage(
        self, account: str, logins: list[str], start: date, end: date
    ) -> list[SeatUsage]:
        specs = {spec["login"]: spec for spec in self._seat_specs()}
        today = self._today()
        rows = []
        day = start
        while day < end:
            for login in logins:
                spec = specs.get(login, {})
                base = PROFILES.get(spec.get("profile", "regular"), 14)
                last_active = today - timedelta(days=int(spec.get("idle_days", 0) or 0))
                if base == 0 or day > last_active or day > today:
                    continue
                weekend = day.weekday() >= 5
                volume = base * (0.15 if weekend else 1.0) * (0.4 + 1.2 * _unit(login, day))
                requests = Decimal(str(round(volume, 0)))
                if requests <= 0:
                    continue
                split = {}
                remaining = float(requests)
                for index, model in enumerate(MODELS):
                    share = remaining if index == len(MODELS) - 1 else round(
                        float(requests) * (0.55, 0.25, 0.12)[index], 0
                    )
                    share = min(share, remaining)
                    if share > 0:
                        split[model] = share
                    remaining -= share
                rows.append(SeatUsage(login=login, day=day, premium_requests=requests, by_model=split))
            day += timedelta(days=1)
        return rows
