"""Presentation helpers for the A2A Gateway templates."""

from __future__ import annotations

import json

from django import template

from integrations.base.a2a import parts_text

register = template.Library()

_DELIVERY_STYLES = {"delivered": "success", "blocked": "danger", "unavailable": "secondary", "failed": "danger"}

_STATE_STYLES = {
    "submitted": "info",
    "working": "primary",
    "input-required": "warning",
    "auth-required": "warning",
    "completed": "success",
    "canceled": "secondary",
    "failed": "danger",
    "rejected": "danger",
    "unknown": "secondary",
}

_SKILL_STYLES = {"pending_review": "warning", "enabled": "success", "disabled": "secondary", "removed": "dark"}

_CLASSIFICATION_STYLES = {"public": "success", "internal": "info", "confidential": "warning", "restricted": "danger"}

_LIMIT_LABELS = {
    "data_classification": "Data above the ceiling",
    "size": "Message too large",
    "loop": "Work sent back round",
    "hops": "Chain too long",
    "rate": "Contract rate",
}


@register.filter
def delivery_style(value: str) -> str:
    return _DELIVERY_STYLES.get(value, "secondary")


@register.filter
def task_state_style(value: str) -> str:
    return _STATE_STYLES.get(value, "secondary")


@register.filter
def skill_status_style(value: str) -> str:
    return _SKILL_STYLES.get(value, "secondary")


@register.filter
def classification_style(value: str) -> str:
    return _CLASSIFICATION_STYLES.get(value, "secondary")


@register.filter
def limit_label(value: str) -> str:
    return _LIMIT_LABELS.get(value, value.replace("_", " ").capitalize() if value else "")


@register.filter
def message_text(value) -> str:
    """The readable content of an A2A message or artifact."""
    if isinstance(value, dict):
        return parts_text(value.get("parts"))
    return ""


@register.filter
def pretty_json(value) -> str:
    return json.dumps(value, indent=2, default=str, sort_keys=True)
