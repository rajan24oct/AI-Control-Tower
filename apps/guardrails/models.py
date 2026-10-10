"""Runtime guardrails: content checks on what agents send to models and to each other.

A guardrail policy names one detector, where it runs (the prompt going in, the
completion coming out, or both), what happens on a hit (block, redact or warn)
and which traffic it covers: IRGF tiers, environments and gateways. Every hit
is recorded as a guardrail event, with the matched text masked, so the record
of a leaked secret does not leak it again.

This is IRGF M14 (runtime controls) in practice: the controls the tier calls
for, applied at the point of use rather than only at design time.
"""

from __future__ import annotations

from django.db import models

from apps.core.models import OrganizationOwnedModel, OrganizationScopedModel


class Detector(models.TextChoices):
    PROMPT_INJECTION = "prompt_injection", "Prompt injection"
    PII = "pii", "Personal data"
    SECRETS = "secrets", "Secrets and credentials"
    DENIED_TOPICS = "denied_topics", "Restricted topics"


class Stage(models.TextChoices):
    INPUT = "input", "Input (prompt)"
    OUTPUT = "output", "Output (completion)"
    BOTH = "both", "Input and output"


class GuardAction(models.TextChoices):
    BLOCK = "block", "Block"
    REDACT = "redact", "Redact"
    WARN = "warn", "Warn and record"


class Source(models.TextChoices):
    AI_GATEWAY = "ai_gateway", "AI Gateway"
    A2A = "a2a", "A2A Gateway"
    PLAYGROUND = "playground", "Playground"


#: The strictest action wins when several policies hit the same text.
ACTION_RANK = {GuardAction.WARN: 0, GuardAction.REDACT: 1, GuardAction.BLOCK: 2}


class GuardrailPolicy(OrganizationScopedModel):
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    detector = models.CharField(max_length=32, choices=Detector.choices)
    stage = models.CharField(max_length=16, choices=Stage.choices, default=Stage.INPUT)
    action = models.CharField(max_length=16, choices=GuardAction.choices, default=GuardAction.BLOCK)
    config = models.JSONField(
        default=dict, blank=True,
        help_text="Detector settings: {\"threshold\": 0.6} for prompt injection, {\"kinds\": [...]} for personal "
                  "data, {\"terms\": [...]} for restricted topics.",
    )
    tiers = models.JSONField(default=list, blank=True, help_text="IRGF tiers covered; empty means every tier.")
    environments = models.JSONField(default=list, blank=True, help_text="Empty means every environment.")
    sources = models.JSONField(default=list, blank=True, help_text="Gateways covered; empty means every gateway.")

    class Meta:
        ordering = ["detector", "name"]
        verbose_name_plural = "guardrail policies"

    def __str__(self) -> str:
        return self.name

    def covers(self, *, stage: str, source: str, tier: int | None, environment: str) -> bool:
        if self.stage != Stage.BOTH and self.stage != stage:
            return False
        if self.sources and source != Source.PLAYGROUND and source not in self.sources:
            return False
        if self.environments and environment and environment not in self.environments:
            return False
        # An agent not yet classified is held to every tier's guardrails: the
        # cautious reading of a missing classification.
        if self.tiers and tier is not None and int(tier) not in {int(t) for t in self.tiers}:
            return False
        return True

    @property
    def scope_label(self) -> str:
        parts = [f"Tier {', '.join(str(t) for t in sorted(self.tiers))}" if self.tiers else "All tiers"]
        if self.environments:
            parts.append(", ".join(e.title() for e in self.environments))
        if self.sources:
            parts.append(", ".join(dict(Source.choices).get(s, s) for s in self.sources))
        return " · ".join(parts)


class GuardrailEvent(OrganizationOwnedModel):
    policy = models.ForeignKey(GuardrailPolicy, null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="events")
    policy_name = models.CharField(max_length=200)
    detector = models.CharField(max_length=32, choices=Detector.choices, db_index=True)
    stage = models.CharField(max_length=16, choices=Stage.choices)
    action = models.CharField(max_length=16, choices=GuardAction.choices, db_index=True)
    source = models.CharField(max_length=16, choices=Source.choices, db_index=True)
    agent = models.ForeignKey("agents.AIAgent", null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="guardrail_events")
    environment = models.CharField(max_length=32, blank=True)
    #: [{"kind", "label", "snippet"}]: what matched, masked.
    findings = models.JSONField(default=list, blank=True)
    score = models.FloatField(null=True, blank=True)
    reference = models.CharField(max_length=200, blank=True, help_text="Trace or task the content belonged to.")

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["organization", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.get_detector_display()} · {self.get_action_display()}"

    @property
    def blocked(self) -> bool:
        return self.action == GuardAction.BLOCK

    @property
    def kinds_label(self) -> str:
        return ", ".join(sorted({f.get("label", f.get("kind", "")) for f in self.findings}))
