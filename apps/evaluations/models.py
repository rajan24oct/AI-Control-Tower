"""Evaluation Centre: evidence for the approval decision.

This is deliberately not an evaluation *platform*. Section 40 puts an advanced
evaluation framework out of scope, and the thing this platform actually needs is
narrower and more answerable: before a model is marked approved for use, or
after a provider changes something underneath you, can anyone show that it still
behaves? A suite is a set of cases with deterministic scorers, and a run is the
evidence.

Two consequences follow from being a governance control rather than a research
tool:

  * **No model grades another model.** Every scorer is deterministic and
    inspectable -- substring, regex, schema, refusal, personal-data absence,
    latency and cost ceilings. An eval whose verdict depends on a second model's
    opinion is not evidence you can put in front of a risk committee.
  * **A run never changes an approval by itself.** It records what happened and
    who ran it; a human still decides. Automatic approval on a green run would
    make the control a rubber stamp with extra steps.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models

from apps.core.constants import Environment
from apps.core.models import OrganizationOwnedModel, OrganizationScopedModel


class TargetKind(models.TextChoices):
    MODEL = "model", "Model"
    AGENT = "agent", "Agent"


class CaseCategory(models.TextChoices):
    """What a case is checking, which is what makes a failure readable.

    A quality failure and a safety failure are different events: one means the
    answer was wrong, the other means the model did something it must not.
    """

    QUALITY = "quality", "Quality"
    SAFETY = "safety", "Safety"
    RED_TEAM = "red_team", "Red team"
    FORMAT = "format", "Format"
    PERFORMANCE = "performance", "Performance"


class Scorer(models.TextChoices):
    CONTAINS = "contains", "Output contains"
    NOT_CONTAINS = "not_contains", "Output does not contain"
    REGEX = "regex", "Output matches pattern"
    REFUSAL = "refusal", "Model refuses"
    JSON_SCHEMA = "json_schema", "Output is JSON with keys"
    NO_PII = "no_pii", "Output carries no personal data"
    MAX_LATENCY = "max_latency", "Responds within"
    MAX_COST = "max_cost", "Costs no more than"


#: Configuration keys each scorer understands. Anything else is rejected at save
#: time, exactly as with control policies -- a typo must not silently turn a
#: check into one that always passes.
SCORER_SCHEMA: dict[str, set[str]] = {
    Scorer.CONTAINS: {"values", "match_all", "case_sensitive"},
    Scorer.NOT_CONTAINS: {"values", "case_sensitive"},
    Scorer.REGEX: {"pattern", "flags"},
    Scorer.REFUSAL: {"markers"},
    Scorer.JSON_SCHEMA: {"required_keys"},
    Scorer.NO_PII: {"kinds"},
    Scorer.MAX_LATENCY: {"milliseconds"},
    Scorer.MAX_COST: {"amount"},
}


class RunStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
)


class EvaluationSuite(OrganizationScopedModel):
    """A named set of cases, and the bar they have to clear."""

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    target_kind = models.CharField(
        max_length=20, choices=TargetKind.choices, default=TargetKind.MODEL
    )
    environment = models.CharField(
        max_length=20, choices=Environment.choices, default=Environment.TEST,
        help_text="Environment recorded against runs of this suite.",
    )

    pass_threshold = models.PositiveSmallIntegerField(
        default=90, help_text="Weighted score, as a percentage, a run must reach to pass."
    )
    #: A suite can pass on points while failing something that must never fail.
    #: Safety and red-team cases are the usual reason to set this.
    blocking_categories = models.JSONField(
        default=list, blank=True,
        help_text="Categories where any failure fails the whole run, whatever the score.",
    )

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="owned_evaluation_suites",
    )

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"], name="uniq_eval_suite_name_per_org"
            )
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def case_count(self) -> int:
        return self.cases.count()

    @property
    def blocking(self) -> set[str]:
        return set(self.blocking_categories or [])


class EvaluationCase(OrganizationOwnedModel):
    """One prompt and the check its answer has to satisfy."""

    suite = models.ForeignKey(
        EvaluationSuite, on_delete=models.CASCADE, related_name="cases"
    )
    name = models.CharField(max_length=200)
    category = models.CharField(
        max_length=20, choices=CaseCategory.choices, default=CaseCategory.QUALITY, db_index=True
    )
    order = models.PositiveSmallIntegerField(default=0)

    prompt = models.TextField()
    system_prompt = models.TextField(blank=True)

    scorer = models.CharField(max_length=30, choices=Scorer.choices, default=Scorer.CONTAINS)
    configuration = models.JSONField(default=dict, blank=True)
    weight = models.PositiveSmallIntegerField(
        default=1, help_text="Relative weight in the suite's score."
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["order", "name"]
        indexes = [models.Index(fields=["suite", "order"])]

    def __str__(self) -> str:
        return self.name

    def clean(self):
        from django.core.exceptions import ValidationError

        allowed = SCORER_SCHEMA.get(self.scorer, set())
        unknown = set(self.configuration or {}) - allowed
        if unknown:
            raise ValidationError(
                {
                    "configuration": (
                        f"'{self.get_scorer_display()}' does not understand: "
                        f"{', '.join(sorted(unknown))}. Allowed: "
                        f"{', '.join(sorted(allowed)) or 'no settings'}."
                    )
                }
            )

    def save(self, *args, **kwargs):
        self.full_clean(exclude=["organization"])
        super().save(*args, **kwargs)


class EvaluationRun(OrganizationOwnedModel):
    """One execution of a suite against one target."""

    suite = models.ForeignKey(
        EvaluationSuite, on_delete=models.CASCADE, related_name="runs"
    )
    model = models.ForeignKey(
        "models_registry.AIModel", null=True, blank=True, on_delete=models.CASCADE,
        related_name="evaluation_runs",
    )
    agent = models.ForeignKey(
        "agents.AIAgent", null=True, blank=True, on_delete=models.CASCADE,
        related_name="evaluation_runs",
    )

    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="triggered_evaluation_runs",
    )
    triggered_by_system = models.CharField(max_length=200, blank=True)

    status = models.CharField(
        max_length=20, choices=RunStatus.choices, default=RunStatus.QUEUED, db_index=True
    )
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    total_cases = models.PositiveIntegerField(default=0)
    completed_cases = models.PositiveIntegerField(default=0)
    passed_cases = models.PositiveIntegerField(default=0)

    score = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal("0"),
        help_text="Weighted percentage of cases passed.",
    )
    passed = models.BooleanField(default=False)
    #: Separate from `passed` so the reason is legible: a run can clear the
    #: score bar and still fail because a safety case failed.
    blocked_by_category = models.CharField(max_length=20, blank=True)

    total_cost = models.DecimalField(max_digits=14, decimal_places=6, default=Decimal("0"))
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "-created_at"]),
            models.Index(fields=["organization", "status"]),
            # Serves the approval gate, which looks up a model's latest
            # completed run on every gateway call.
            models.Index(
                fields=["model", "status", "-finished_at"], name="eval_latest_for_model_idx"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.suite.name} vs {self.target_label}"

    @property
    def target(self):
        return self.model or self.agent

    @property
    def target_label(self) -> str:
        if self.model_id:
            return self.model.model_name
        if self.agent_id:
            return self.agent.name
        return "no target"

    @property
    def is_finished(self) -> bool:
        return self.status in TERMINAL_RUN_STATUSES

    @property
    def progress(self) -> int:
        if not self.total_cases:
            return 0
        return int(self.completed_cases / self.total_cases * 100)

    @property
    def failed_cases(self) -> int:
        return max(0, self.completed_cases - self.passed_cases)

    @property
    def verdict(self) -> str:
        if not self.is_finished:
            return "pending"
        if self.blocked_by_category:
            return "blocked"
        return "passed" if self.passed else "failed"


class EvaluationResult(OrganizationOwnedModel):
    """One case's outcome, with the answer that produced it."""

    run = models.ForeignKey(EvaluationRun, on_delete=models.CASCADE, related_name="results")
    case = models.ForeignKey(
        EvaluationCase, null=True, on_delete=models.SET_NULL, related_name="results"
    )
    case_name = models.CharField(max_length=200)
    category = models.CharField(max_length=20, choices=CaseCategory.choices)

    passed = models.BooleanField(default=False)
    detail = models.CharField(max_length=500, blank=True)
    #: Kept so a failure can be read without re-running it. Subject to the same
    #: prompt-visibility controls as any other output the platform stores.
    output = models.TextField(blank=True)

    execution = models.ForeignKey(
        "executions.Execution", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="evaluation_results",
    )
    latency_ms = models.PositiveIntegerField(null=True, blank=True)
    cost = models.DecimalField(max_digits=14, decimal_places=6, default=Decimal("0"))
    weight = models.PositiveSmallIntegerField(default=1)
    error_message = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["run", "passed"])]

    def __str__(self) -> str:
        return f"{self.case_name}: {'pass' if self.passed else 'fail'}"
