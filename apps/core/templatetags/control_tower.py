"""Presentation helpers shared by every Control Tower template."""

from __future__ import annotations

from django import template
from django.utils.html import format_html

from apps.core.constants import HEALTH_STATUS_STYLES, RISK_LEVEL_STYLES

register = template.Library()

_STATUS_STYLES = {
    "active": "success",
    "draft": "secondary",
    "pending_review": "warning",
    "inactive": "secondary",
    "deprecated": "warning",
    "retired": "dark",
}

_SEVERITY_STYLES = {
    "critical": "danger",
    "high": "warning",
    "medium": "info",
    "info": "secondary",
}

_SEVERITY_ICONS = {
    "critical": "bi-exclamation-octagon-fill",
    "high": "bi-exclamation-triangle-fill",
    "medium": "bi-info-circle-fill",
    "info": "bi-dot",
}

_ENVIRONMENT_STYLES = {
    "production": "danger",
    "staging": "warning",
    "test": "info",
    "development": "secondary",
    "sandbox": "secondary",
}


@register.filter
def risk_style(value: str) -> str:
    return RISK_LEVEL_STYLES.get(value, "secondary")


@register.filter
def health_style(value: str) -> str:
    return HEALTH_STATUS_STYLES.get(value, "secondary")


@register.filter
def status_style(value: str) -> str:
    return _STATUS_STYLES.get(value, "secondary")


@register.filter
def severity_style(value: str) -> str:
    return _SEVERITY_STYLES.get(value, "secondary")


@register.filter
def severity_icon(value: str) -> str:
    return _SEVERITY_ICONS.get(value, "bi-dot")


@register.filter
def environment_style(value: str) -> str:
    return _ENVIRONMENT_STYLES.get(value, "secondary")


@register.filter
def autonomy_style(value) -> str:
    """Higher autonomy reads hotter -- level 4 should never look routine."""
    try:
        level = int(value)
    except (TypeError, ValueError):
        return "secondary"
    return {0: "success", 1: "success", 2: "info", 3: "warning", 4: "danger"}.get(level, "secondary")


@register.filter
def tier_style(value) -> str:
    """Badge tone for an IRGF risk tier; Tier 4 reads as critical."""
    try:
        tier = int(value)
    except (TypeError, ValueError):
        return "secondary"
    return {1: "success", 2: "info", 3: "warning", 4: "danger"}.get(tier, "secondary")


@register.filter
def tier_tone(value) -> str:
    """Card tone for an IRGF risk tier, matching the Control Tower's risk colours."""
    try:
        tier = int(value)
    except (TypeError, ValueError):
        return "low"
    return {1: "low", 2: "moderate", 3: "high", 4: "critical"}.get(tier, "low")


_GATE_STATUS_STYLES = {
    "authorized": "success", "active": "success", "approved": "success", "accepted": "success",
    "pending": "warning", "awaiting_g4": "warning", "awaiting_confirmation": "warning",
    "suspended": "danger", "rejected": "danger",
    "revoked": "secondary", "expired": "secondary", "superseded": "secondary", "reverted": "secondary",
}

_CHANGE_CLASS_STYLES = {"cosmetic": "secondary", "minor": "info", "material": "warning", "major": "danger"}


@register.filter
def gate_status_style(value: str) -> str:
    """Badge tone for IRGF G3, G4 and M17 record statuses."""
    return _GATE_STATUS_STYLES.get(value, "secondary")


@register.filter
def change_class_style(value: str) -> str:
    return _CHANGE_CLASS_STYLES.get(value, "secondary")


_DRIFT_STATUS_STYLES = {
    "open": "warning", "ratifying": "info", "excepting": "info", "excepted": "secondary", "resolved": "success",
}
_VERIFICATION_STYLES = {"conforms": "success", "diverges": "danger", "ungoverned": "secondary"}


@register.filter
def drift_status_style(value: str) -> str:
    """Badge tone for an IRGF M13 drift record's status."""
    return _DRIFT_STATUS_STYLES.get(value, "secondary")


@register.filter
def verification_style(value: str) -> str:
    return _VERIFICATION_STYLES.get(value, "secondary")


@register.filter
def duration_ms(value) -> str:
    """Milliseconds as the unit a person would say: 850 ms, 4.2 s, 3 min, 2 h."""
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return "—"
    if ms < 1000:
        return f"{ms} ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f} s"
    if seconds < 3600:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


@register.filter
def duration_s(value) -> str:
    try:
        return duration_ms(int(value) * 1000)
    except (TypeError, ValueError):
        return "—"


def _is_agent(subject) -> bool:
    return hasattr(subject, "agent_id") and hasattr(subject, "autonomy_level")


@register.filter
def system_kind(subject) -> str:
    """IRGF governs systems: an agent, or an application and its agents."""
    return "agent" if _is_agent(subject) else "application"


@register.filter
def system_url(subject) -> str:
    """The inventory page of an agent or an application."""
    from django.urls import reverse

    if subject is None:
        return ""
    name = "inventory:agent_detail" if _is_agent(subject) else "inventory:application_detail"
    return reverse(name, args=[subject.pk])


@register.filter
def authorize_url(subject) -> str:
    """Where to ask Gate G3 to authorize an agent or an application."""
    from django.urls import reverse

    if subject is None:
        return ""
    name = "lifecycle:authorization_request" if _is_agent(subject) else "lifecycle:application_authorization_request"
    return reverse(name, args=[subject.pk])


@register.filter
def isodate(value):
    """An ISO timestamp stored in JSON, as a datetime the date filter can format."""
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


_EXECUTION_STATUS_STYLES = {
    "queued": "secondary",
    "running": "primary",
    "waiting_for_approval": "info",
    "completed": "success",
    "failed": "danger",
    "cancelled": "secondary",
    "timed_out": "warning",
}

_APPROVAL_STYLES = {
    "not_required": "secondary",
    "pending": "warning",
    "approved": "success",
    "rejected": "danger",
    "expired": "secondary",
    "cancelled": "secondary",
}

#: Timeline presentation. Grouping by phase keeps a 40-event run readable:
#: model calls read blue, tool calls purple, approvals amber, failures red.
_EVENT_PRESENTATION = {
    "EXECUTION_STARTED": ("bi-play-circle-fill", "primary"),
    "PROMPT_CREATED": ("bi-chat-left-text", "secondary"),
    "MODEL_REQUEST_STARTED": ("bi-cpu", "info"),
    "MODEL_REQUEST_COMPLETED": ("bi-cpu-fill", "info"),
    "TOOL_SELECTED": ("bi-wrench", "purple"),
    "TOOL_CALL_STARTED": ("bi-wrench-adjustable", "purple"),
    "TOOL_CALL_COMPLETED": ("bi-wrench-adjustable-circle-fill", "purple"),
    "AGENT_STARTED": ("bi-robot", "primary"),
    "AGENT_STEP_STARTED": ("bi-arrow-right-circle", "secondary"),
    "AGENT_STEP_COMPLETED": ("bi-check-circle", "secondary"),
    "APPROVAL_REQUESTED": ("bi-person-raised-hand", "warning"),
    "APPROVAL_GRANTED": ("bi-person-check-fill", "success"),
    "APPROVAL_REJECTED": ("bi-person-x-fill", "danger"),
    "POLICY_VIOLATION": ("bi-shield-exclamation", "danger"),
    "ERROR": ("bi-exclamation-octagon-fill", "danger"),
    "EXECUTION_COMPLETED": ("bi-flag-fill", "success"),
    "EXECUTION_CANCELLED": ("bi-slash-circle", "secondary"),
}


@register.filter
def execution_status_style(value: str) -> str:
    return _EXECUTION_STATUS_STYLES.get(value, "secondary")


@register.filter
def approval_style(value: str) -> str:
    return _APPROVAL_STYLES.get(value, "secondary")


_EFFECT_STYLES = {
    "deny": "danger",
    "require_approval": "warning",
    "warn": "info",
}


@register.filter
def effect_style(value: str) -> str:
    return _EFFECT_STYLES.get(value, "secondary")


_NOTIFICATION_PRESENTATION = {
    "critical": ("bi-exclamation-octagon-fill", "danger"),
    "warning": ("bi-exclamation-triangle-fill", "warning"),
    "info": ("bi-info-circle-fill", "primary"),
}


@register.filter
def notification_icon(value: str) -> str:
    return _NOTIFICATION_PRESENTATION.get(value, ("bi-bell-fill", "secondary"))[0]


@register.filter
def notification_style(value: str) -> str:
    return _NOTIFICATION_PRESENTATION.get(value, ("bi-bell-fill", "secondary"))[1]


@register.filter
def event_icon(value: str) -> str:
    return _EVENT_PRESENTATION.get(value, ("bi-dot", "secondary"))[0]


@register.filter
def event_style(value: str) -> str:
    return _EVENT_PRESENTATION.get(value, ("bi-dot", "secondary"))[1]


@register.filter
def percentage(value, places: int = 1) -> str:
    """Render a 0..1 ratio as a percentage.

    Ratios are stored as fractions so they stay comparable in aggregates; the
    conversion belongs at the point of display, not in the service that
    computed them.
    """
    try:
        return f"{float(value) * 100:.{int(places)}f}%"
    except (TypeError, ValueError):
        return "—"


@register.simple_tag(takes_context=True)
def query_replace(context, **kwargs):
    """Rebuild the query string with some parameters replaced.

    Keeps filters intact while paginating.
    """
    request = context["request"]
    params = request.GET.copy()
    for key, value in kwargs.items():
        if value in (None, ""):
            params.pop(key, None)
        else:
            params[key] = value
    encoded = params.urlencode()
    return f"?{encoded}" if encoded else ""


@register.simple_tag
def page_window(page_obj, on_each_side=2, on_ends=1):
    """Page numbers around the current page, with the gaps elided.

    For page 7 of 20: 1 … 5 6 7 8 9 … 20. A gap comes back as None, so the
    template can tell it from a number without comparing translated text.
    """
    ellipsis = page_obj.paginator.ELLIPSIS
    return [
        None if page == ellipsis else page
        for page in page_obj.paginator.get_elided_page_range(
            page_obj.number, on_each_side=on_each_side, on_ends=on_ends
        )
    ]


@register.simple_tag(takes_context=True)
def sort_th(context, label, key, css="", first="asc"):
    """A column header that sorts the whole list, not just the rows on screen.

    Clicking toggles ascending and descending. The first click goes `first`:
    "desc" suits dates, money and counts, where the largest is what matters.
    Filters are kept and the page resets -- page 4 of a new order means nothing.
    """
    request = context["request"]
    current = request.GET.get("sort", "")
    active = current.lstrip("-") == key
    descending = active and current.startswith("-")
    if active:
        target = key if descending else f"-{key}"
    else:
        target = f"-{key}" if first == "desc" else key

    params = request.GET.copy()
    params["sort"] = target
    params.pop("page", None)

    if active:
        icon, state = ("bi-caret-down-fill", "descending") if descending else ("bi-caret-up-fill", "ascending")
    else:
        icon, state = "bi-chevron-expand", "none"
    return format_html(
        '<th class="{}" aria-sort="{}"><a class="ct-sort{}" href="?{}">{}'
        '<i class="bi {}" aria-hidden="true"></i></a></th>',
        css, state, " is-active" if active else "", params.urlencode(), label, icon,
    )
