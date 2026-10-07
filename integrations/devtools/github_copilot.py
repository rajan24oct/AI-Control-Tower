"""GitHub Copilot billing adapter.

Reads two things from the GitHub REST API:

  Seats      GET /orgs/{org}/copilot/billing/seats
             Who holds a seat, on which plan, which team granted it, and when
             they last used it.
  Usage      GET /organizations/{org}/settings/billing/premium_request/usage
             Premium requests, asked per user and per day (`user`, `year`,
             `month`, `day`), so spend can be attributed to the individual.

The token needs the `manage_billing:copilot` or `read:org` scope for seats,
and organization billing read access for usage. Stdlib only, like the other
HTTP adapters.

Per-user, per-day usage is one call per seat per day. `max_usage_calls`
(default 2,000) bounds a single sync so a large estate cannot exhaust the
hourly API allowance; a sync that hits the bound says so and the next one
continues from the last day it has.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from integrations.base.devtools import BaseDeveloperToolAdapter, SeatRecord, SeatUsage
from integrations.base.exceptions import AdapterConfigurationError, ProviderUnavailable
from integrations.registry import register_cost

API_VERSION = "2022-11-28"
DEFAULT_ENDPOINT = "https://api.github.com"
PREMIUM_SKU_MARKER = "premium request"


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_date(value: str | None) -> date | None:
    parsed = _parse_datetime(value)
    if parsed is not None:
        return parsed.date()
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


class UsageCallLimitReached(ProviderUnavailable):
    """A sync asked for more per-day usage than `max_usage_calls` allows."""


@register_cost("github_copilot")
class GitHubCopilotAdapter(BaseDeveloperToolAdapter):
    adapter_key = "github_copilot"
    display_name = "GitHub Copilot"
    platform = "github_copilot"

    # -- HTTP -----------------------------------------------------------------

    @property
    def base_url(self) -> str:
        return (self.config.endpoint or DEFAULT_ENDPOINT).rstrip("/")

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        token = self.config.resolve_credential()
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "ai-control-tower",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                return json.loads(response.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("message", "")
            except Exception:
                pass
            if exc.code in (401, 403):
                raise AdapterConfigurationError(
                    f"GitHub refused the token ({exc.code}): {detail or 'check its scopes'}."
                ) from exc
            if exc.code == 404:
                raise ProviderUnavailable(
                    f"GitHub has no such resource at {path} (404). Check the organization login, "
                    "that Copilot is enabled for it, and that it uses the enhanced billing platform."
                ) from exc
            raise ProviderUnavailable(f"GitHub API error {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ProviderUnavailable(f"GitHub API unreachable: {exc}") from exc

    # -- Seats ----------------------------------------------------------------

    def list_seats(self, account: str) -> list[SeatRecord]:
        if not account:
            raise AdapterConfigurationError("A GitHub organization login is required.")

        seats: list[SeatRecord] = []
        page = 1
        while True:
            body = self._get(
                f"/orgs/{urllib.parse.quote(account)}/copilot/billing/seats",
                {"per_page": 100, "page": page},
            ) or {}
            batch = body.get("seats") or []
            for item in batch:
                assignee = item.get("assignee") or {}
                login = assignee.get("login")
                if not login:
                    continue
                team = item.get("assigning_team") or {}
                plan = (item.get("plan_type") or "business").lower()
                seats.append(
                    SeatRecord(
                        login=login,
                        plan=plan if plan in ("business", "enterprise") else "business",
                        assigned_on=_parse_date(item.get("created_at")),
                        display_name=assignee.get("name") or "",
                        email=assignee.get("email") or "",
                        assigning_team=team.get("slug") or team.get("name") or "",
                        last_activity_at=_parse_datetime(item.get("last_activity_at")),
                        last_activity_editor=item.get("last_activity_editor") or "",
                        pending_cancellation_on=_parse_date(item.get("pending_cancellation_date")),
                    )
                )
            if len(batch) < 100:
                return seats
            page += 1

    # -- Usage ----------------------------------------------------------------

    def get_seat_usage(
        self, account: str, logins: list[str], start: date, end: date
    ) -> list[SeatUsage]:
        limit = int(self.config.option("max_usage_calls", 2000))
        days = [start + timedelta(days=offset) for offset in range((end - start).days)]
        if len(days) * len(logins) > limit:
            raise UsageCallLimitReached(
                f"Reading {len(logins)} seats over {len(days)} days needs {len(days) * len(logins)} "
                f"calls, over the limit of {limit}. Sync a shorter window or raise max_usage_calls."
            )

        path = f"/organizations/{urllib.parse.quote(account)}/settings/billing/premium_request/usage"
        rows: list[SeatUsage] = []
        for login in logins:
            for day in days:
                body = self._get(
                    path, {"user": login, "year": day.year, "month": day.month, "day": day.day}
                ) or {}
                total = Decimal("0")
                by_model: dict[str, float] = {}
                for item in body.get("usageItems") or []:
                    if PREMIUM_SKU_MARKER not in str(item.get("sku", "")).lower():
                        continue
                    quantity = Decimal(str(item.get("grossQuantity") or 0))
                    total += quantity
                    model = item.get("model") or "unspecified"
                    by_model[model] = by_model.get(model, 0.0) + float(quantity)
                if total:
                    rows.append(SeatUsage(login=login, day=day, premium_requests=total, by_model=by_model))
        return rows
