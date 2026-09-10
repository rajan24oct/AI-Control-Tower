"""Provider domain services (section 23)."""

from __future__ import annotations

import logging

from django.utils import timezone

from apps.audit.models import AuditAction
from apps.audit.services import AuditService
from apps.core.constants import HealthStatus
from apps.providers.models import AIProvider
from integrations.base.exceptions import AdapterError

logger = logging.getLogger(__name__)


class ProviderHealthService:
    """Probes providers through their adapters and records the outcome."""

    @staticmethod
    def check(provider: AIProvider) -> AIProvider:
        previous_status = provider.health_status

        try:
            result = provider.get_adapter().health_check()
            status = HealthStatus.HEALTHY if result.healthy else HealthStatus.UNHEALTHY
            message = result.message or ""
            latency = result.latency_ms
        except AdapterError as exc:
            status = HealthStatus.UNHEALTHY
            message = str(exc)[:500]
            latency = None
        except Exception as exc:
            logger.exception("Unexpected error checking provider %s", provider.pk)
            status = HealthStatus.UNKNOWN
            message = f"Health check raised {type(exc).__name__}: {exc}"[:500]
            latency = None

        provider.health_status = status
        provider.last_health_check = timezone.now()
        provider.last_health_message = message
        provider.last_health_latency_ms = latency
        provider.save(
            update_fields=[
                "health_status",
                "last_health_check",
                "last_health_message",
                "last_health_latency_ms",
                "updated_at",
            ]
        )

        if previous_status != status:
            AuditService.log(
                action=AuditAction.HEALTH_CHECK,
                resource_type=AIProvider._meta.label,
                resource_id=provider.pk,
                resource_label=provider.name,
                organization=provider.organization,
                old_value={"health_status": previous_status},
                new_value={"health_status": status},
                metadata={"message": message},
            )

            # Only a transition is worth telling anyone about; a provider that
            # stays down does not need to notify on every sweep.
            from apps.notifications import emitters

            emitters.provider_health_changed(provider, previous_status)

        return provider

    @classmethod
    def check_all(cls, queryset=None) -> dict[str, int]:
        queryset = queryset if queryset is not None else AIProvider.objects.all()
        counts: dict[str, int] = {}
        for provider in queryset.iterator():
            cls.check(provider)
            counts[provider.health_status] = counts.get(provider.health_status, 0) + 1
        return counts


class ProviderCapabilityService:
    """Keeps the cached capability list in step with the adapter."""

    @staticmethod
    def refresh(provider: AIProvider) -> list[str]:
        try:
            capabilities = provider.get_adapter().capability_list()
        except AdapterError:
            return list(provider.supported_capabilities or [])

        provider.supported_capabilities = capabilities
        provider.save(update_fields=["supported_capabilities", "updated_at"])
        return capabilities
