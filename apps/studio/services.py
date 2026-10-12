"""Solution Studio: generating blueprints, linking them to intake, and running the sandbox."""

from __future__ import annotations

import time
import uuid
from decimal import Decimal

from django.db import transaction

from apps.accounts.roles import Perm
from apps.audit.models import AuditAction
from apps.audit.services import AuditService
from apps.core.constants import Environment
from apps.studio import advisor
from apps.studio.models import (
    RequestStatus,
    RunStatus,
    SandboxRun,
    SandboxSession,
    SolutionBlueprint,
    SolutionRequest,
)

#: Comparing more models than this side by side stops being readable.
MAX_COMPARE = 3
MAX_INPUT_CHARS = 8000
MAX_OUTPUT_TOKENS = 2000


class StudioError(Exception):
    pass


def can_use_sandbox(user, organization) -> bool:
    return user.has_org_permission(organization, Perm.GATEWAY_INVOKE)


def _audit(user, organization, action, obj, label, changes=None):
    AuditService.log(action=action, resource_type=obj._meta.label, resource_id=obj.pk, resource_label=label,
                     organization=organization, actor=user, new_value=changes or {})


class StudioService:
    @staticmethod
    def generate(request: SolutionRequest, user, guidance: str = "") -> SolutionBlueprint:
        """Ask the advisor for a blueprint and keep it as the request's next version."""
        organization = request.organization
        try:
            advice = advisor.advise(request, organization, guidance)
        except advisor.AdvisorError as exc:
            request.status, request.last_error = RequestStatus.FAILED, str(exc)[:1000]
            request.save(update_fields=["status", "last_error", "updated_at"])
            raise StudioError(str(exc)) from exc
        with transaction.atomic():
            version = (request.blueprints.order_by("-version").values_list("version", flat=True).first() or 0) + 1
            blueprint = SolutionBlueprint.objects.create(
                organization=organization, request=request, version=version, generated_by=advice.generated_by,
                archetype=advice.archetype, content=advice.content, guidance=guidance[:2000],
                input_tokens=advice.input_tokens, output_tokens=advice.output_tokens, created_by=user,
            )
            request.status = RequestStatus.READY
            request.last_error = advice.fallback_reason[:1000]
            request.save(update_fields=["status", "last_error", "updated_at"])
        _audit(user, organization, AuditAction.CREATE, blueprint, f"Blueprint {blueprint}",
               {"generated_by": advice.generated_by, "archetype": advice.archetype})
        return blueprint

    @staticmethod
    def from_use_case(use_case, user) -> SolutionRequest:
        """A solution request filled in from an intake use case."""
        existing = use_case.solution_requests.order_by("-created_at").first()
        if existing is not None:
            return existing
        return SolutionRequest.objects.create(
            organization=use_case.organization, use_case=use_case, title=use_case.title,
            problem="\n\n".join(p for p in [use_case.need, use_case.summary,
                                            f"Decision supported: {use_case.decision_supported}"
                                            if use_case.decision_supported else ""] if p),
            users=use_case.affected_parties, data_sources=use_case.data_path,
            constraints=use_case.sector_constraints, data_classification=use_case.data_classification,
            human_in_loop=(use_case.intended_autonomy or 1) <= 2, created_by=user,
        )

    @staticmethod
    def propose(request: SolutionRequest, user):
        """A draft intake use case from the request, carrying the blueprint's provisional scores."""
        from apps.intake.models import UseCase
        from apps.intake.services import can_propose

        if request.use_case_id:
            return request.use_case
        if not can_propose(user, request.organization):
            raise StudioError("Your role does not allow proposing use cases.")
        blueprint = request.blueprint
        scores = (blueprint.content.get("governance", {}).get("scores") if blueprint else {}) or {}
        field_for = {"d1": "d1_consequence", "d2": "d2_autonomy", "d3": "d3_reversibility", "d4": "d4_exposure",
                     "d5_data": "d5_data_sensitivity", "d5_model": "d5_model_uncertainty"}
        with transaction.atomic():
            use_case = UseCase.objects.create(
                organization=request.organization, title=request.title[:200],
                summary=(blueprint.content.get("summary", "") if blueprint else "")[:1000],
                need=request.problem, affected_parties=request.users, data_path=request.data_sources,
                sector_constraints=request.constraints, data_classification=request.data_classification,
                intended_autonomy=2 if request.human_in_loop else 3, created_by=user,
                **{field_for[k]: int(v) for k, v in scores.items() if k in field_for and v},
            )
            request.use_case = use_case
            request.save(update_fields=["use_case", "updated_at"])
        _audit(user, request.organization, AuditAction.CREATE, use_case, f"Use case {use_case}",
               {"from_solution_request": request.reference})
        return use_case

    @staticmethod
    def open_sandbox(blueprint: SolutionBlueprint, user) -> SandboxSession:
        from apps.models_registry.models import AIModel

        sandbox = blueprint.content.get("sandbox", {})
        wanted = [pk for pk in sandbox.get("models", [])][:MAX_COMPARE]
        valid = [str(pk) for pk in AIModel.objects.for_organization(blueprint.organization)
                 .filter(pk__in=[w for w in wanted if _is_uuid(w)]).values_list("pk", flat=True)]
        return SandboxSession.objects.create(
            organization=blueprint.organization, name=f"{blueprint.request.reference} · {blueprint.request.title}"[:200],
            owner=user, blueprint=blueprint, system_prompt=sandbox.get("system_prompt", ""),
            sample_inputs=sandbox.get("sample_inputs", []), model_ids=valid, created_by=user,
        )


def _is_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False


# -- Sandbox ----------------------------------------------------------------------------------


def session_models(session: SandboxSession):
    from apps.models_registry.models import AIModel

    ids = [pk for pk in session.model_ids if _is_uuid(pk)]
    found = {str(m.pk): m for m in AIModel.objects.for_organization(session.organization)
             .filter(pk__in=ids).select_related("provider")}
    return [found[pk] for pk in ids if pk in found]


class SandboxService:
    @staticmethod
    def run(session: SandboxSession, user, text: str) -> list[SandboxRun]:
        """One input against each of the session's models, through the AI Gateway in the sandbox."""
        from apps.gateway.services import GatewayError, GatewayRequest, GatewayService

        text = (text or "").strip()
        if not text:
            raise StudioError("Write something to send.")
        if len(text) > MAX_INPUT_CHARS:
            raise StudioError(f"Inputs are limited to {MAX_INPUT_CHARS:,} characters in the sandbox.")
        if not can_use_sandbox(user, session.organization):
            raise StudioError("Your role does not allow calling models through the gateway.")
        models = session_models(session)
        if not models:
            raise StudioError("Pick at least one model to try.")

        batch, runs = uuid.uuid4(), []
        messages = ([{"role": "system", "content": session.system_prompt}] if session.system_prompt else []) + \
            [{"role": "user", "content": text}]
        for model in models[:MAX_COMPARE]:
            request = GatewayRequest(
                agent=session.agent, messages=[dict(m) for m in messages], model=model.model_id,
                model_type=model.model_type, environment=Environment.SANDBOX,
                max_tokens=min(session.max_tokens or 600, MAX_OUTPUT_TOKENS),
                temperature=float(session.temperature), trace_id=f"sandbox-{batch}",
                metadata={"source": "sandbox", "session": str(session.pk), "user": str(user.pk)},
            )
            run = SandboxRun(organization=session.organization, session=session, batch=batch,
                             requested_model=model, input_text=text, created_by=user)
            started = time.monotonic()
            try:
                response = GatewayService.complete(session.organization, request)
            except GatewayError as exc:
                run.status = RunStatus.BLOCKED if exc.status_code in (202, 402, 403) else RunStatus.FAILED
                run.error_code, run.error = exc.code, exc.message[:2000]
                run.guardrails = exc.detail if exc.code == "guardrail_blocked" else {}
                run.latency_ms = int((time.monotonic() - started) * 1000)
            else:
                run.status = RunStatus.COMPLETED
                run.output_text = response.output_text
                run.served_by = f"{response.model.model_name} · {response.model.provider.name}"
                run.input_tokens, run.output_tokens = response.input_tokens, response.output_tokens
                run.cost = response.cost or Decimal("0")
                run.latency_ms = response.latency_ms
                if request.metadata.get("guardrail_redacted"):
                    run.guardrails = {"action": "redact", "note": "Part of the input was redacted by a guardrail."}
            run.save()
            runs.append(run)
        session.save(update_fields=["updated_at"])
        return runs
