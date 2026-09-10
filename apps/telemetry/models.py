"""Raw telemetry storage (section 14).

Raw payloads are kept separately from the normalized execution records. That
separation matters: when an engine's format changes or a normalization bug is
found, the original evidence is still on disk and can be replayed.
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone

from apps.core.models import OrganizationOwnedModel


class IngestionStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSED = "processed", "Processed"
    FAILED = "failed", "Failed"
    REJECTED = "rejected", "Rejected"


class RawTelemetryPayload(OrganizationOwnedModel):
    received_at = models.DateTimeField(default=timezone.now, db_index=True)
    endpoint = models.CharField(max_length=200, blank=True)
    adapter_key = models.CharField(max_length=64, blank=True)

    service_account = models.ForeignKey(
        "api.ServiceAccount",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="telemetry_payloads",
    )
    execution = models.ForeignKey(
        "executions.Execution",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="raw_payloads",
    )

    payload = models.JSONField(default=dict)
    status = models.CharField(
        max_length=20, choices=IngestionStatus.choices, default=IngestionStatus.PENDING, db_index=True
    )
    processing_error = models.TextField(blank=True)
    event_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-received_at"]
        verbose_name = "Raw telemetry payload"
        indexes = [
            models.Index(fields=["organization", "-received_at"]),
            models.Index(fields=["status", "-received_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.adapter_key or 'unknown'} @ {self.received_at:%Y-%m-%d %H:%M:%S}"
