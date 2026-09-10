"""Serializers for execution telemetry (section 5).

The ingest serializers accept *loose* identifiers -- an agent's business id, a
model's provider-side name -- because that is what an external engine actually
knows. Resolution to Control Tower records happens in the service layer.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.executions.models import (
    ApprovalState,
    Execution,
    ExecutionEvent,
    ExecutionStatusChoices,
    EventTypeChoices,
)
from apps.core.constants import Environment


class ExecutionEventSerializer(serializers.ModelSerializer):
    event_id = serializers.UUIDField(source="id", read_only=True)
    event_type_display = serializers.CharField(source="get_event_type_display", read_only=True)

    class Meta:
        model = ExecutionEvent
        fields = [
            "event_id",
            "event_type",
            "event_type_display",
            "timestamp",
            "sequence",
            "duration_ms",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cost",
            "model_id",
            "status",
            "error_message",
            "trace_id",
            "metadata",
        ]


class ExecutionSerializer(serializers.ModelSerializer):
    execution_id = serializers.CharField(source="external_id", read_only=True)
    trace_id = serializers.CharField(source="trace.trace_id", read_only=True, default=None)
    agent_name = serializers.CharField(source="agent.name", read_only=True, default=None)
    application_name = serializers.CharField(source="application.name", read_only=True, default=None)
    provider_name = serializers.CharField(source="provider.name", read_only=True, default=None)
    model_name = serializers.CharField(source="model.model_name", read_only=True, default=None)
    duration_ms = serializers.IntegerField(read_only=True)
    event_count = serializers.IntegerField(source="events.count", read_only=True)

    class Meta:
        model = Execution
        fields = [
            "id",
            "execution_id",
            "organization",
            "trace_id",
            "agent",
            "agent_name",
            "application",
            "application_name",
            "provider",
            "provider_name",
            "model",
            "model_name",
            "framework",
            "user",
            "source_adapter",
            "started_at",
            "ended_at",
            "latency_ms",
            "duration_ms",
            "status",
            "environment",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cost",
            "currency",
            "cost_is_derived",
            "error_message",
            "approval_required",
            "approval_status",
            "event_count",
            "metadata",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class EventIngestSerializer(serializers.Serializer):
    """One normalized event submitted by an external engine."""

    event_id = serializers.UUIDField(required=False)
    event_type = serializers.ChoiceField(choices=EventTypeChoices.choices)
    timestamp = serializers.DateTimeField(required=False)
    sequence = serializers.IntegerField(required=False, min_value=0)
    duration_ms = serializers.IntegerField(required=False, min_value=0, allow_null=True)
    input_tokens = serializers.IntegerField(required=False, min_value=0, default=0)
    output_tokens = serializers.IntegerField(required=False, min_value=0, default=0)
    cost = serializers.DecimalField(
        max_digits=14, decimal_places=6, required=False, default=0
    )
    model_id = serializers.CharField(required=False, allow_blank=True, max_length=200)
    status = serializers.CharField(required=False, allow_blank=True, max_length=32)
    error_message = serializers.CharField(required=False, allow_blank=True)
    trace_id = serializers.CharField(required=False, allow_blank=True, max_length=200)
    metadata = serializers.JSONField(required=False, default=dict)


class ExecutionIngestSerializer(serializers.Serializer):
    """Create or update an execution.

    Idempotent on `execution_id`: re-sending the same run updates it, which is
    what at-least-once delivery from a queue requires.
    """

    execution_id = serializers.CharField(required=False, allow_blank=True, max_length=200)
    trace_id = serializers.CharField(required=False, allow_blank=True, max_length=200)

    agent = serializers.CharField(required=False, allow_blank=True, max_length=200)
    application = serializers.CharField(required=False, allow_blank=True, max_length=200)
    provider = serializers.CharField(required=False, allow_blank=True, max_length=200)
    model = serializers.CharField(required=False, allow_blank=True, max_length=200)

    framework = serializers.CharField(required=False, allow_blank=True, max_length=40)
    environment = serializers.ChoiceField(choices=Environment.choices, required=False)
    status = serializers.ChoiceField(choices=ExecutionStatusChoices.choices, required=False)

    started_at = serializers.DateTimeField(required=False)
    ended_at = serializers.DateTimeField(required=False, allow_null=True)
    latency_ms = serializers.IntegerField(required=False, min_value=0, allow_null=True)

    input_tokens = serializers.IntegerField(required=False, min_value=0)
    output_tokens = serializers.IntegerField(required=False, min_value=0)
    cost = serializers.DecimalField(max_digits=14, decimal_places=6, required=False)
    currency = serializers.CharField(required=False, max_length=3)

    error_message = serializers.CharField(required=False, allow_blank=True)
    approval_required = serializers.BooleanField(required=False)
    approval_status = serializers.ChoiceField(choices=ApprovalState.choices, required=False)
    metadata = serializers.JSONField(required=False)

    events = EventIngestSerializer(many=True, required=False)


class ExecutionFinishSerializer(serializers.Serializer):
    """Closing payload for the complete / fail / cancel endpoints."""

    ended_at = serializers.DateTimeField(required=False)
    latency_ms = serializers.IntegerField(required=False, min_value=0, allow_null=True)
    input_tokens = serializers.IntegerField(required=False, min_value=0)
    output_tokens = serializers.IntegerField(required=False, min_value=0)
    cost = serializers.DecimalField(max_digits=14, decimal_places=6, required=False)
    currency = serializers.CharField(required=False, max_length=3)
    error_message = serializers.CharField(required=False, allow_blank=True)
    reason = serializers.CharField(required=False, allow_blank=True)


class WebhookIngestSerializer(serializers.Serializer):
    """A whole engine payload, normalized by a named execution adapter."""

    adapter = serializers.CharField(required=False, allow_blank=True, max_length=64)
    payload = serializers.JSONField()
