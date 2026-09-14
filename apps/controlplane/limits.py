"""Agent chain link 06: value, rate and counterparty limits, enforced before an action.

A tool reviewer records which argument of a tool carries a monetary value and
which names the counterparty (`MCPTool.value_argument` and
`counterparty_argument`). The MCP Gateway reads them from every call and holds
the calling agent to its declared action limits before the call goes out.
Limits are preventive: a call over a limit is refused, never sent and then
reported. The Control Plane checks the same limits again after an action, for
anything that reached a tool some other way (link 07).

Values are compared in the currency of the agent's limits; a tool whose
amounts are in another currency needs converting before it is mapped.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db.models import Sum
from django.utils import timezone

LIMIT_LABELS = {
    "max_value_per_action": "Value per action",
    "max_value_per_day": "Value per day",
    "max_actions_per_hour": "Actions per hour",
    "allowed_counterparties": "Allowed counterparties",
}


@dataclass
class Breach:
    limit: str
    reason: str


def _decimal(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return amount if amount.is_finite() else None


def action_facts(tool, arguments) -> tuple[Decimal | None, str, str]:
    """The value and counterparty a call carries, and any problem reading them."""
    arguments = arguments if isinstance(arguments, dict) else {}
    value, counterparty, problem = None, "", ""
    name = getattr(tool, "value_argument", "")
    if name and arguments.get(name) not in (None, ""):
        value = _decimal(arguments[name])
        if value is None:
            problem = f"'{name}' is not a number."
    name = getattr(tool, "counterparty_argument", "")
    if name and arguments.get(name) not in (None, ""):
        counterparty = str(arguments[name])[:255]
    return value, counterparty, problem


def _reached(agent):
    from apps.mcp_gateway.models import CallStatus, MCPToolCall

    return MCPToolCall.objects.filter(
        agent=agent, status__in=(CallStatus.SUCCEEDED, CallStatus.TOOL_ERROR)
    )


def start_of_day(now=None):
    return timezone.localtime(now or timezone.now()).replace(hour=0, minute=0, second=0, microsecond=0)


def usage(agent, now=None) -> dict:
    """Where the agent stands against its rate and daily limits right now."""
    now = now or timezone.now()
    calls = _reached(agent)
    return {
        "actions_last_hour": calls.filter(created_at__gte=now - timedelta(hours=1)).count(),
        "value_today": calls.filter(created_at__gte=start_of_day(now)).aggregate(
            total=Sum("action_value")
        )["total"] or Decimal("0"),
    }


def check(agent, tool, arguments, *, now=None) -> Breach | None:
    """The first of the agent's limits this call would break, or None."""
    limits = (getattr(agent, "action_limits", None) or {}) if agent is not None else {}
    if not limits:
        return None
    unit = f" {limits['currency']}" if limits.get("currency") else ""
    value, counterparty, problem = action_facts(tool, arguments)

    per_action = _decimal(limits.get("max_value_per_action"))
    per_day = _decimal(limits.get("max_value_per_day"))
    if problem and (per_action is not None or per_day is not None):
        return Breach(
            "max_value_per_action",
            f"Action limit (link 06): {problem} The call cannot be held to the agent's value limits.",
        )
    if value is not None and per_action is not None and value > per_action:
        return Breach(
            "max_value_per_action",
            f"Action limit (link 06): {value}{unit} is above the agent's limit of {per_action}{unit} per action.",
        )

    allowed = limits.get("allowed_counterparties") or []
    if allowed and counterparty and counterparty.casefold() not in {party.casefold() for party in allowed}:
        return Breach(
            "allowed_counterparties",
            f"Action limit (link 06): '{counterparty}' is not one of the agent's allowed counterparties.",
        )

    now = now or timezone.now()
    rate = limits.get("max_actions_per_hour")
    if rate:
        recent = _reached(agent).filter(created_at__gte=now - timedelta(hours=1)).count()
        if recent >= int(rate):
            return Breach(
                "max_actions_per_hour",
                f"Action limit (link 06): {recent} actions in the last hour; the agent's limit is {rate}.",
            )

    if value is not None and per_day is not None:
        spent = _reached(agent).filter(created_at__gte=start_of_day(now)).aggregate(
            total=Sum("action_value")
        )["total"] or Decimal("0")
        if spent + value > per_day:
            return Breach(
                "max_value_per_day",
                f"Action limit (link 06): {spent + value}{unit} today would pass the agent's daily "
                f"limit of {per_day}{unit}.",
            )
    return None


def exceeded(agent, tool, arguments) -> Breach | None:
    """After the fact: a completed call's own value or counterparty over a limit.

    Rate and daily totals are enforced before a call; checking them again here
    would count the call being verified against itself.
    """
    limits = (getattr(agent, "action_limits", None) or {}) if agent is not None else {}
    if not limits:
        return None
    value, counterparty, _ = action_facts(tool, arguments)
    per_action = _decimal(limits.get("max_value_per_action"))
    if value is not None and per_action is not None and value > per_action:
        return Breach("max_value_per_action", f"{value} went through against a limit of {per_action} per action.")
    allowed = limits.get("allowed_counterparties") or []
    if allowed and counterparty and counterparty.casefold() not in {party.casefold() for party in allowed}:
        return Breach("allowed_counterparties", f"'{counterparty}' is not an allowed counterparty.")
    return None
