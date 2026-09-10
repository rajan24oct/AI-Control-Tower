"""Request-scoped audit context.

Services deep in the call stack need to know who is acting without every
function signature carrying an actor argument. Context variables give us that
safely, including under async views.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AuditContext:
    actor: Any = None
    actor_label: str = ""
    ip_address: str | None = None
    user_agent: str = ""
    source: str = "system"
    metadata: dict = field(default_factory=dict)


_current: contextvars.ContextVar[AuditContext] = contextvars.ContextVar(
    "audit_context", default=AuditContext()
)


def get_audit_context() -> AuditContext:
    return _current.get()


def set_audit_context(context: AuditContext):
    return _current.set(context)


def reset_audit_context(token) -> None:
    _current.reset(token)
