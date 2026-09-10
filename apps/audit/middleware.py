"""Populates the audit context from the incoming request."""

from __future__ import annotations

from apps.audit.context import AuditContext, reset_audit_context, set_audit_context
from apps.audit.models import AuditSource


def client_ip(request) -> str | None:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


class AuditContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        actor = user if (user and user.is_authenticated) else None

        if request.path.startswith("/api/"):
            source = AuditSource.API
        elif request.path.startswith("/admin/"):
            source = AuditSource.ADMIN
        else:
            source = AuditSource.UI

        token = set_audit_context(
            AuditContext(
                actor=actor,
                actor_label=str(actor) if actor else "anonymous",
                ip_address=client_ip(request),
                user_agent=request.META.get("HTTP_USER_AGENT", "")[:1000],
                source=source,
            )
        )
        try:
            return self.get_response(request)
        finally:
            reset_audit_context(token)
