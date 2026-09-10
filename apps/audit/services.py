"""Audit service -- the single entry point for writing the audit trail."""

from __future__ import annotations

import datetime
import decimal
import uuid
from typing import Any

from django.db import models

from apps.audit.context import get_audit_context
from apps.audit.models import AuditLog, AuditSource


def _jsonify(value: Any) -> Any:
    """Coerce a model field value into something JSON can hold."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (uuid.UUID, decimal.Decimal)):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, models.Model):
        return str(value.pk)
    if isinstance(value, dict):
        return {k: _jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonify(v) for v in value]
    return str(value)


def snapshot(instance: models.Model, fields: list[str] | None = None) -> dict:
    """JSON-safe snapshot of a model instance, for old_value / new_value."""
    data = {}
    for field in instance._meta.concrete_fields:
        if fields is not None and field.name not in fields:
            continue
        data[field.name] = _jsonify(getattr(instance, field.attname, None))
    return data


#: Housekeeping fields that move on every save and mean nothing to a reader.
#: `updated_at` in particular would otherwise appear in every single diff.
NOISE_FIELDS = frozenset({"updated_at", "created_at", "updated_by", "created_by"})


def diff(before: dict | None, after: dict | None, ignore=NOISE_FIELDS) -> tuple[dict, dict]:
    """Reduce two snapshots to only what actually changed.

    An audit entry listing forty unchanged fields buries the one that moved.
    """
    before = before or {}
    after = after or {}
    changed = [
        key
        for key in set(before) | set(after)
        if key not in ignore and before.get(key) != after.get(key)
    ]
    return (
        {key: before.get(key) for key in sorted(changed)},
        {key: after.get(key) for key in sorted(changed)},
    )


class AuditService:
    """Writes immutable audit entries.

    Auditing must never take down the operation being audited, so failures are
    swallowed after being surfaced to the logger.
    """

    @staticmethod
    def log(
        *,
        action: str,
        resource_type: str,
        resource_id: str | uuid.UUID = "",
        resource_label: str = "",
        organization=None,
        actor=None,
        old_value: dict | None = None,
        new_value: dict | None = None,
        metadata: dict | None = None,
        source: str | None = None,
    ) -> AuditLog | None:
        context = get_audit_context()
        actor = actor if actor is not None else context.actor
        actor_label = str(actor) if actor is not None else (context.actor_label or "system")

        return AuditLog.objects.create(
            organization=organization,
            actor=actor if actor is not None and getattr(actor, "pk", None) else None,
            actor_label=actor_label,
            source=source or context.source or AuditSource.SYSTEM,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id or ""),
            resource_label=resource_label,
            ip_address=context.ip_address,
            user_agent=context.user_agent,
            old_value=old_value,
            new_value=new_value,
            metadata={**context.metadata, **(metadata or {})},
        )

    @classmethod
    def log_change(
        cls,
        *,
        action: str,
        instance: models.Model,
        before: dict | None = None,
        organization=None,
        actor=None,
        metadata: dict | None = None,
    ) -> AuditLog | None:
        """Convenience wrapper that snapshots the instance as `new_value`."""
        organization = organization or getattr(instance, "organization", None)
        return cls.log(
            action=action,
            resource_type=instance._meta.label,
            resource_id=instance.pk,
            resource_label=str(instance),
            organization=organization,
            actor=actor,
            old_value=before,
            new_value=snapshot(instance),
            metadata=metadata,
        )
