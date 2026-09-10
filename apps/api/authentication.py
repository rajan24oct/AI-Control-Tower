"""API-key authentication for external execution engines."""

from __future__ import annotations

from django.utils import timezone
from rest_framework import authentication, exceptions

from apps.api.models import APIKey
from apps.audit.context import AuditContext, set_audit_context
from apps.audit.models import AuditSource

HEADER = "HTTP_X_API_KEY"
AUTH_KEYWORD = "api-key"


class ServiceAccountAuthentication(authentication.BaseAuthentication):
    """Authenticates `X-API-Key: act_...` or `Authorization: Api-Key act_...`.

    Sets `request.user` to the ServiceAccount, which duck-types the permission
    surface the rest of the platform relies on.
    """

    def authenticate(self, request):
        raw_key = self._extract_key(request)
        if not raw_key:
            return None

        parts = APIKey.split_raw(raw_key)
        if not parts:
            raise exceptions.AuthenticationFailed("Malformed API key.")
        prefix, secret = parts

        api_key = (
            APIKey.objects.select_related("service_account", "service_account__organization")
            .filter(prefix=prefix)
            .first()
        )
        # Uniform failure message: never reveal whether the prefix existed.
        if api_key is None or not api_key.verify(secret):
            raise exceptions.AuthenticationFailed("Invalid API key.")
        if not api_key.is_usable:
            raise exceptions.AuthenticationFailed("API key is revoked or expired.")

        service_account = api_key.service_account
        if not service_account.is_active:
            raise exceptions.AuthenticationFailed("Service account is inactive.")

        APIKey.objects.filter(pk=api_key.pk).update(last_used_at=timezone.now())

        # The audit middleware ran before authentication, so the actor is only
        # knowable now.
        set_audit_context(
            AuditContext(
                actor=None,
                actor_label=f"{service_account.name} (service account)",
                ip_address=request.META.get("REMOTE_ADDR"),
                user_agent=request.META.get("HTTP_USER_AGENT", "")[:1000],
                source=AuditSource.API,
                metadata={
                    "service_account_id": str(service_account.pk),
                    "api_key_prefix": prefix,
                    "external_system": service_account.external_system,
                },
            )
        )

        return (service_account, api_key)

    def authenticate_header(self, request):
        return 'Api-Key realm="ai-control-tower"'

    @staticmethod
    def _extract_key(request) -> str:
        direct = request.META.get(HEADER, "").strip()
        if direct:
            return direct

        header = request.META.get("HTTP_AUTHORIZATION", "").strip()
        if not header:
            return ""
        parts = header.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == AUTH_KEYWORD:
            return parts[1].strip()
        return ""
