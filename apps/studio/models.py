"""AI Solution Studio: from an idea to a technical blueprint, and a sandbox to try it.

A **solution request** is an AI use case described in plain terms: the problem,
who uses it, the data, the constraints. The solution advisor (an LLM, or the
built-in advisor when none is configured) answers it with a **blueprint**: the
approach, a technology stack layer by layer, the build plan, the controls its
IRGF tier calls for, a cost estimate, and a starter system prompt with sample
inputs. Regenerating keeps the earlier versions.

The **sandbox** is where people try the idea against the organization's own
models, through the AI Gateway in the sandbox environment, so policies,
guardrails and budgets apply and every call is recorded like any other.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from apps.core.constants import DataClassification
from apps.core.models import OrganizationOwnedModel


def next_reference(model, organization_id, prefix: str) -> str:
    last = model.objects.filter(organization_id=organization_id).order_by("-created_at") \
        .values_list("reference", flat=True).first()
    number = int(last.split("-")[1]) + 1 if last and "-" in last else 1
    while model.objects.filter(organization_id=organization_id, reference=f"{prefix}-{number:04d}").exists():
        number += 1
    return f"{prefix}-{number:04d}"


class Volume(models.TextChoices):
    LOW = "low", "Low · under 1,000 requests a day"
    MEDIUM = "medium", "Medium · 1,000–50,000 a day"
    HIGH = "high", "High · over 50,000 a day"


class Hosting(models.TextChoices):
    ANY = "any", "No preference"
    AZURE = "azure", "Microsoft Azure"
    AWS = "aws", "Amazon Web Services"
    GCP = "gcp", "Google Cloud"
    ON_PREM = "on_prem", "On-premises / private"


class RequestStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    READY = "ready", "Blueprint ready"
    FAILED = "failed", "Advisor failed"


class SolutionRequest(OrganizationOwnedModel):
    reference = models.CharField(max_length=20, editable=False, db_index=True)
    title = models.CharField(max_length=200)
    problem = models.TextField(help_text="The problem or opportunity, and what good looks like.")
    users = models.TextField(blank=True, help_text="Who will use it, and who is affected by its output.")
    data_sources = models.TextField(blank=True, help_text="Where the data comes from: systems, documents, APIs.")
    integrations = models.TextField(blank=True, help_text="Systems it must read from or write to.")
    constraints = models.TextField(blank=True, help_text="Regulation, latency, accuracy, budget, languages.")
    data_classification = models.CharField(max_length=32, choices=DataClassification.choices,
                                           default=DataClassification.INTERNAL)
    volume = models.CharField(max_length=16, choices=Volume.choices, default=Volume.LOW)
    hosting = models.CharField("Hosting preference", max_length=16, choices=Hosting.choices, default=Hosting.ANY)
    human_in_loop = models.BooleanField("A person reviews outputs before they take effect", default=True)
    use_case = models.ForeignKey("intake.UseCase", null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="solution_requests")
    status = models.CharField(max_length=16, choices=RequestStatus.choices, default=RequestStatus.DRAFT,
                              db_index=True)
    last_error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "reference"], name="uniq_solution_request_reference")
        ]

    def __str__(self) -> str:
        return f"{self.reference} {self.title}"

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = next_reference(SolutionRequest, self.organization_id, "SR")
        super().save(*args, **kwargs)

    @property
    def blueprint(self):
        return self.blueprints.order_by("-version").first()


class SolutionBlueprint(OrganizationOwnedModel):
    request = models.ForeignKey(SolutionRequest, on_delete=models.CASCADE, related_name="blueprints")
    version = models.PositiveIntegerField(default=1)
    #: "openai:gpt-4o-mini", or "builtin" for the rule-based advisor.
    generated_by = models.CharField(max_length=100)
    archetype = models.CharField(max_length=64, blank=True)
    content = models.JSONField(default=dict)
    guidance = models.TextField(blank=True, help_text="What the person asked the advisor to change.")
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-version"]
        constraints = [models.UniqueConstraint(fields=["request", "version"], name="uniq_blueprint_version")]

    def __str__(self) -> str:
        return f"{self.request.reference} v{self.version}"

    @property
    def by_llm(self) -> bool:
        return self.generated_by != "builtin"

    @property
    def generator_label(self) -> str:
        if not self.by_llm:
            return "Built-in advisor"
        provider, _, model = self.generated_by.partition(":")
        return f"{'ChatGPT' if provider == 'openai' else provider} · {model}"


# -- Sandbox -----------------------------------------------------------------------------------


class SandboxSession(OrganizationOwnedModel):
    name = models.CharField(max_length=200)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                              related_name="sandbox_sessions")
    blueprint = models.ForeignKey(SolutionBlueprint, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="sandbox_sessions")
    agent = models.ForeignKey("agents.AIAgent", null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="sandbox_sessions",
                              help_text="Optional: run as this agent, so its policies and guardrails apply.")
    system_prompt = models.TextField(blank=True)
    #: Model primary keys compared side by side (up to three).
    model_ids = models.JSONField(default=list, blank=True)
    temperature = models.DecimalField(max_digits=3, decimal_places=2, default=0.2)
    max_tokens = models.PositiveIntegerField(default=600)
    sample_inputs = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return self.name


class RunStatus(models.TextChoices):
    COMPLETED = "completed", "Completed"
    BLOCKED = "blocked", "Blocked"
    FAILED = "failed", "Failed"


class SandboxRun(OrganizationOwnedModel):
    session = models.ForeignKey(SandboxSession, on_delete=models.CASCADE, related_name="runs")
    batch = models.UUIDField(db_index=True, help_text="Runs of one input across the compared models.")
    requested_model = models.ForeignKey("models_registry.AIModel", null=True, on_delete=models.SET_NULL,
                                        related_name="+")
    served_by = models.CharField(max_length=200, blank=True)
    input_text = models.TextField()
    output_text = models.TextField(blank=True)
    status = models.CharField(max_length=16, choices=RunStatus.choices)
    error_code = models.CharField(max_length=64, blank=True)
    error = models.TextField(blank=True)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cost = models.DecimalField(max_digits=12, decimal_places=6, default=0)
    latency_ms = models.PositiveIntegerField(default=0)
    guardrails = models.JSONField(default=dict, blank=True)
    rating = models.SmallIntegerField(null=True, blank=True, help_text="1 good, -1 poor.")

    class Meta:
        ordering = ["-created_at"]

    @property
    def style(self) -> str:
        return {RunStatus.COMPLETED: "success", RunStatus.BLOCKED: "warning"}.get(self.status, "danger")
