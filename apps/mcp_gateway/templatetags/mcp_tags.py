"""Presentation helpers for the MCP Gateway templates."""

from __future__ import annotations

import json

from django import template

register = template.Library()

_CALL_STYLES = {
    "succeeded": "success",
    "tool_error": "warning",
    "blocked": "danger",
    "unavailable": "secondary",
    "failed": "danger",
}

_TOOL_STYLES = {
    "pending_review": "warning",
    "enabled": "success",
    "disabled": "secondary",
    "removed": "dark",
}


@register.filter
def call_status_style(value: str) -> str:
    return _CALL_STYLES.get(value, "secondary")


@register.filter
def tool_status_style(value: str) -> str:
    return _TOOL_STYLES.get(value, "secondary")


@register.filter
def circuit_style(value: str) -> str:
    return {"open": "danger", "half_open": "warning"}.get(value, "success")


@register.filter
def as_json(value) -> str:
    """Arguments as the caller sent them, not as a Python repr."""
    return json.dumps(value, default=str, sort_keys=True)
