"""Presentation helpers shared by every Control Tower template."""

from __future__ import annotations

from django import template

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
