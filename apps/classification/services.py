"""Scoring, countersigning and re-score triggers for IRGF S2 classifications."""

from __future__ import annotations

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditAction
from apps.audit.services import AuditService
from apps.classification import irgf
from apps.classification.models import (
    CURRENT_STATUSES,
    ClassificationStatus,
    RiskClassification,
)


class ClassificationError(ValueError):
    """A scoring or countersigning request the rules do not allow."""


def _subject_kwargs(subject) -> dict:
    from apps.agents.models import AIAgent

    return {"agent": subject} if isinstance(subject, AIAgent) else {"application": subject}


def _owner_ids(subject) -> set:
    """People in the subject's delivery line, who may not countersign it."""
    ids = {
        getattr(subject, name, None)
        for name in ("owner_id", "business_owner_id", "technical_owner_id")
    }
    return {pk for pk in ids if pk}


def _message(error: ValidationError) -> str:
    if hasattr(error, "message_dict"):
        return " ".join(message for messages in error.message_dict.values() for message in messages)
    return " ".join(error.messages)


class ClassificationService:
    # -- Reading ------------------------------------------------------------

    @staticmethod
    def current(subject) -> RiskClassification | None:
        if subject is None or subject.pk is None:
            return None
        return (
            subject.risk_classifications.filter(status__in=CURRENT_STATUSES)
            .select_related("scored_by", "countersigned_by")
            .order_by("-scored_at")
            .first()
        )

    @staticmethod
    def history(subject):
        return subject.risk_classifications.select_related(
            "scored_by", "countersigned_by"
        ).order_by("-scored_at")

    @classmethod
    def suggested_scores(cls, subject) -> dict[str, int]:
        """A starting point for the scorer, never a substitute for scoring.

        Starts from the current record if there is one. Registry facts only ever
        raise a suggestion: an agent whose autonomy went up gets a higher D2,
        but nothing here quietly lowers a score someone already reasoned about.
        """
        current = cls.current(subject)
        scores = current.scores() if current else {
            "d1_consequence": 2,
            "d2_autonomy": 1,
            "d3_reversibility": 2,
            "d4_exposure": 2,
            "d5_data_sensitivity": 2,
            "d5_model_uncertainty": irgf.DEFAULT_D5_MODEL,
        }

        autonomy = getattr(subject, "autonomy_level", None)
        if autonomy is not None:
            scores["d2_autonomy"] = max(scores["d2_autonomy"], irgf.AUTONOMY_TO_D2.get(autonomy, 1))

        data = irgf.CLASSIFICATION_TO_D5_DATA.get(getattr(subject, "data_classification", ""))
        if data:
            scores["d5_data_sensitivity"] = max(scores["d5_data_sensitivity"], data)
        return scores

    # -- Scoring (S2) -------------------------------------------------------

    @classmethod
    @transaction.atomic
    def score(
        cls,
        subject,
        *,
        scores: dict[str, int],
        rationale: str,
        actor=None,
        tier_override: int | None = None,
        override_rationale: str = "",
    ) -> RiskClassification:
        """Record a scoring. It stands as provisional D1–D5 until countersigned."""
        missing = [field for field in irgf.ALL_FIELDS if field not in scores]
        if missing:
            raise ClassificationError(f"Score every dimension; missing {', '.join(missing)}.")

        user = actor if getattr(actor, "pk", None) and not getattr(actor, "is_service_account", False) else None
        record = RiskClassification(
            organization=subject.organization,
            **_subject_kwargs(subject),
            **{field: scores[field] for field in irgf.ALL_FIELDS},
            rationale=(rationale or "").strip(),
            tier_override=tier_override or None,
            override_rationale=(override_rationale or "").strip(),
            scored_by=user,
            scored_at=timezone.now(),
            created_by=user,
        )
        try:
            record.clean()
        except ValidationError as exc:
            raise ClassificationError(_message(exc)) from None

        previous = cls.current(subject)
        record.save()
        if previous is not None:
            previous.status = ClassificationStatus.SUPERSEDED
            previous.superseded_at = record.scored_at
            previous.superseded_by = record
            previous.save(update_fields=["status", "superseded_at", "superseded_by", "updated_at"])

        cls._sync_subject(subject, record)

        AuditService.log(
            action=AuditAction.RISK_CLASSIFICATION,
            resource_type=RiskClassification._meta.label,
            resource_id=record.pk,
            resource_label=str(record),
            organization=subject.organization,
            actor=actor,
            old_value={"tier": previous.tier, "status": previous.status} if previous else None,
            new_value={"tier": record.tier, "status": record.status, **record.scores()},
            metadata={
                "stage": irgf.STAGE,
                "gate_evidence": irgf.PROVISIONAL_GATE,
                "subject": str(subject),
                "override": record.tier_override,
                "supersedes": str(previous.pk) if previous else "",
            },
        )

        from apps.notifications import emitters

        emitters.classification_needs_countersignature(record)
        return record

    # -- Countersigning (M6) ------------------------------------------------

    @staticmethod
    def independence_problem(record: RiskClassification, user) -> str:
        """Why this person may not countersign, or an empty string."""
        if user is None or not getattr(user, "pk", None) or getattr(user, "is_service_account", False):
            return "A person must countersign; a service account cannot vouch for a score."
        if record.scored_by_id and user.pk == record.scored_by_id:
            return "The person who scored a system cannot countersign it (IRGF M6)."
        if user.pk in _owner_ids(record.subject):
            return (
                f"You own {record.subject}, which puts you in its delivery line. "
                "IRGF M6 needs a countersigner from outside it."
            )
        return ""

    @classmethod
    @transaction.atomic
    def countersign(
        cls,
        record: RiskClassification,
        *,
        actor,
        adjusted_scores: dict[str, int] | None = None,
        note: str = "",
    ) -> RiskClassification:
        """Confirm the tier, optionally adjusting scores with a recorded reason."""
        if not record.is_provisional:
            raise ClassificationError(
                f"Only a provisional classification can be countersigned; this one is "
                f"{record.get_status_display().lower()}."
            )
        problem = cls.independence_problem(record, actor)
        if problem:
            raise ClassificationError(problem)

        original = record.scores()
        changes = {
            field: value
            for field, value in (adjusted_scores or {}).items()
            if field in original and original[field] != value
        }
        note = (note or "").strip()
        if changes and not note:
            raise ClassificationError("Record why you changed the scores; the reason is part of the evidence.")

        previous_tier = record.tier
        if changes:
            record.scorer_scores = original
            for field, value in changes.items():
                setattr(record, field, value)
            try:
                record.clean()
            except ValidationError as exc:
                raise ClassificationError(_message(exc)) from None

        record.recompute()
        record.status = ClassificationStatus.CONFIRMED
        record.countersigned_by = actor
        record.countersigned_at = timezone.now()
        record.countersign_note = note
        record.review_due = (
            (timezone.now() + timedelta(days=irgf.REVIEW_INTERVAL_DAYS)).date()
            if record.tier >= irgf.ANNUAL_REVIEW_FROM_TIER
            else None
        )
        record.updated_by = actor
        record.save()
        cls._sync_subject(record.subject, record)

        AuditService.log(
            action=AuditAction.RISK_COUNTERSIGN,
            resource_type=RiskClassification._meta.label,
            resource_id=record.pk,
            resource_label=str(record),
            organization=record.organization,
            actor=actor,
            old_value={"tier": previous_tier, **{f: original[f] for f in changes}},
            new_value={"tier": record.tier, "status": record.status, **changes},
            metadata={
                "stage": irgf.STAGE,
                "gate_evidence": irgf.CONFIRMED_GATE,
                "adjusted": bool(changes),
                "note": note,
            },
        )
        return record

    # -- Re-score triggers --------------------------------------------------

    @classmethod
    def flag_rescore(cls, subject, reason: str, *, actor=None) -> RiskClassification | None:
        """Mark the current classification as needing a re-score."""
        record = cls.current(subject)
        if record is None:
            return None
        reasons = list(record.rescore_reasons or [])
        if reason in reasons:
            return record
        record.rescore_required = True
        record.rescore_reasons = [*reasons, reason]
        record.save(update_fields=["rescore_required", "rescore_reasons", "updated_at"])

        AuditService.log(
            action=AuditAction.UPDATE,
            resource_type=RiskClassification._meta.label,
            resource_id=record.pk,
            resource_label=str(record),
            organization=record.organization,
            actor=actor,
            new_value={"rescore_required": True},
            metadata={"stage": irgf.STAGE, "trigger": reason},
        )
        return record

    # -- Subject cache ------------------------------------------------------

    @staticmethod
    def _sync_subject(subject, record: RiskClassification | None) -> None:
        """Keep the tier on the agent or application in step with its record.

        Policies and approval rules read the tier on every gateway call, so it
        is cached on the asset. Written with `update()` so it neither fires the
        registry's save signals nor races a concurrent edit of other fields.
        """
        tier = record.tier if record is not None and record.is_current else None
        queryset = type(subject).objects.filter(pk=subject.pk)
        before = queryset.values_list("risk_tier", flat=True).first()
        queryset.update(risk_tier=tier)
        subject.risk_tier = tier
        if before != tier:
            from apps.classification.signals import tier_changed

            tier_changed.send(sender=type(subject), subject=subject, before=before, after=tier)
