"""Compliance pages: the overview, a system's compliance, and its evidence pack."""

from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import date

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views import View
from django.views.generic import TemplateView

from apps.accounts.roles import Perm
from apps.audit.models import AuditAction, AuditLog
from apps.audit.services import AuditService
from apps.compliance.evidence import Subject
from apps.compliance.frameworks import DISCLAIMER, REGIMES, REQUIREMENTS
from apps.compliance.models import Applicability, Attestation, AttestationStatus, EUCategory, RegulatoryScope
from apps.compliance.services import assess, overview
from apps.core.responses import action_response

DECIDE = (Perm.RISK_MANAGE, Perm.GATE_DECIDE)
EXPORT = (Perm.AUDIT_VIEW, Perm.RISK_MANAGE, Perm.GATE_DECIDE)


def allowed(request, permissions) -> bool:
    organization = request.organization
    return organization is not None and any(request.user.has_org_permission(organization, p) for p in permissions)


def subject_for(request, kind: str, pk) -> Subject:
    from apps.agents.models import AIAgent
    from apps.applications_registry.models import AIApplication

    model = {"agent": AIAgent, "application": AIApplication}.get(kind)
    if model is None:
        raise Http404
    obj = model.objects.for_organization(request.organization).filter(pk=pk).first()
    if obj is None:
        raise Http404("Not found in the active organization.")
    return Subject(obj)


class OverviewView(LoginRequiredMixin, TemplateView):
    template_name = "compliance/overview.html"
    extra_context = {
        "page_title": "Compliance & Evidence",
        "page_subtitle": "EU AI Act, ISO/IEC 42001 and NIST AI RMF, answered from what the platform records",
    }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.organization is not None:
            context.update(overview(self.request.organization))
        context.update({"regimes": list(REGIMES.values()), "disclaimer": DISCLAIMER})
        return context


class SystemView(LoginRequiredMixin, TemplateView):
    template_name = "compliance/system.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        subject = subject_for(self.request, kwargs["kind"], kwargs["pk"])
        context.update({
            "subject": subject,
            "assessments": assess(subject),
            "page_title": subject.name,
            "page_subtitle": f"Compliance · {subject.kind.title()}" + (f" · IRGF Tier {subject.tier}" if subject.tier else ""),
            "eu_categories": EUCategory.choices,
            "attestations": Attestation.objects.filter(**subject.filter).select_related("attested_by"),
            "can_decide": allowed(self.request, DECIDE),
            "can_export": allowed(self.request, EXPORT),
            "disclaimer": DISCLAIMER,
            "today": date.today(),
        })
        return context


class ScopeView(LoginRequiredMixin, View):
    """Record whether a regime applies to a system, and for the EU AI Act, its category."""

    def post(self, request, kind, pk):
        subject = subject_for(request, kind, pk)
        back = reverse("compliance:system", args=[kind, pk])
        if not allowed(request, DECIDE):
            return action_response(request, message="Scope decisions are made by risk and governance roles.",
                                   tone="danger", redirect_to=back, status=403)
        regime = request.POST.get("regime", "")
        if regime not in REGIMES:
            return action_response(request, message="Unknown regime.", tone="danger", redirect_to=back, status=400)
        rationale = request.POST.get("rationale", "").strip()
        if not rationale:
            return action_response(request, message="Record the rationale for the scope decision.", tone="danger",
                                   redirect_to=back, status=400)
        scope = RegulatoryScope.objects.filter(**subject.filter, regime=regime).first() or RegulatoryScope(
            organization=subject.organization, **subject.filter, regime=regime)
        before = {"applicability": scope.applicability, "eu_category": scope.eu_category, "rationale": scope.rationale}
        if regime == "eu_ai_act":
            category = request.POST.get("eu_category", EUCategory.TO_ASSESS)
            if category not in EUCategory.values:
                return action_response(request, message="Unknown category.", tone="danger", redirect_to=back, status=400)
            scope.eu_category = category
            scope.applicability = (Applicability.DOES_NOT_APPLY if category == EUCategory.NOT_IN_SCOPE
                                   else Applicability.APPLIES)
        else:
            scope.applicability = (Applicability.APPLIES if request.POST.get("applicability") == "applies"
                                   else Applicability.DOES_NOT_APPLY)
        scope.rationale, scope.assessed_by, scope.assessed_at = rationale, request.user, timezone.now()
        scope.save()
        AuditService.log(action=AuditAction.CONFIG_CHANGE, resource_type=RegulatoryScope._meta.label,
                         resource_id=subject.obj.pk, resource_label=f"{subject.name} · {REGIMES[regime].short}",
                         organization=subject.organization, actor=request.user, old_value=before,
                         new_value={"applicability": scope.applicability, "eu_category": scope.eu_category,
                                    "rationale": rationale})
        return action_response(request, message=f"Recorded the {REGIMES[regime].short} scope for {subject.name}.",
                               redirect_to=back)


class AttestView(LoginRequiredMixin, View):
    """A person vouching for a requirement the platform cannot see."""

    def post(self, request, kind, pk):
        subject = subject_for(request, kind, pk)
        back = reverse("compliance:system", args=[kind, pk])
        owner = request.user in subject.owners
        if not (owner or allowed(request, DECIDE)):
            return action_response(request, message="Attestations come from the system's owner or a risk or "
                                   "governance role.", tone="danger", redirect_to=back, status=403)
        code = request.POST.get("requirement", "")
        statement = request.POST.get("statement", "").strip()
        status = request.POST.get("status", AttestationStatus.MET)
        if code not in REQUIREMENTS or status not in AttestationStatus.values or not statement:
            return action_response(request, message="Choose the requirement and say what was done.", tone="danger",
                                   redirect_to=back, status=400)
        valid_until = request.POST.get("valid_until") or None
        attestation = Attestation.objects.create(
            organization=subject.organization, **subject.filter, requirement=code, status=status, statement=statement,
            evidence_reference=request.POST.get("evidence_reference", "").strip()[:500], attested_by=request.user,
            valid_until=valid_until, created_by=request.user,
        )
        AuditService.log(action=AuditAction.CREATE, resource_type=Attestation._meta.label, resource_id=attestation.pk,
                         resource_label=f"{code} · {subject.name}", organization=subject.organization,
                         actor=request.user, new_value={"requirement": code, "status": status, "statement": statement,
                                                        "valid_until": valid_until})
        return action_response(request, message=f"Attested {REQUIREMENTS[code].title} for {subject.name}.",
                               redirect_to=back)


# -- The evidence pack -----------------------------------------------------------------


def pack_context(request, subject: Subject) -> dict:
    classification = subject.classification()
    authorization = subject.authorization()
    return {
        "subject": subject,
        "assessments": assess(subject),
        "classification": classification,
        "authorization": authorization,
        "attestations": Attestation.objects.filter(**subject.filter).select_related("attested_by"),
        "audit": AuditLog.objects.filter(resource_id=str(subject.obj.pk)).order_by("-created_at")[:50],
        "generated_at": timezone.now(),
        "generated_by": request.user.get_full_name() or request.user.email,
        "disclaimer": DISCLAIMER,
    }


def pack_json(context: dict) -> dict:
    subject = context["subject"]
    return {
        "system": {"kind": subject.kind, "name": subject.name, "id": str(subject.obj.pk), "irgf_tier": subject.tier,
                   "owners": [p.get_full_name() or p.email for p in subject.owners],
                   "data_classification": subject.data_classification},
        "generated_at": context["generated_at"].isoformat(),
        "generated_by": context["generated_by"],
        "disclaimer": DISCLAIMER,
        "regimes": [{
            "regime": a.regime.name,
            "applies": a.applies,
            "eu_category": a.scope.eu_category or None,
            "scope_rationale": a.scope.rationale,
            "coverage_percent": a.coverage,
            "requirements": [{
                "code": r.requirement.code, "reference": r.requirement.reference, "title": r.requirement.title,
                "status": r.status, "note": r.note,
                "attestation": ({"statement": r.attestation.statement, "evidence": r.attestation.evidence_reference,
                                 "by": str(r.attestation.attested_by), "valid_until": str(r.attestation.valid_until or "")}
                                if r.attestation else None),
                "evidence": [{"check": c.title, "status": c.status, "finding": c.finding,
                              "items": [{"label": i.label, "url": i.url, "when": str(i.when or "")} for i in c.items]}
                             for c in r.checks],
            } for r in a.results],
        } for a in context["assessments"]],
    }


def log_export(request, subject: Subject, fmt: str) -> None:
    AuditService.log(action="export", resource_type="compliance.EvidencePack", resource_id=subject.obj.pk,
                     resource_label=f"Evidence pack · {subject.name}", organization=subject.organization,
                     actor=request.user, new_value={"format": fmt})


class PackView(LoginRequiredMixin, View):
    """The printable evidence pack (HTML; print it to PDF), or the whole pack as a ZIP."""

    def get(self, request, kind, pk, fmt="html"):
        subject = subject_for(request, kind, pk)
        if not allowed(request, EXPORT):
            raise PermissionDenied("Evidence packs include the audit trail: audit, risk or governance roles only.")
        context = pack_context(request, subject)
        log_export(request, subject, fmt)
        if fmt == "html":
            return render(request, "compliance/pack.html", context)

        name = f"evidence-pack-{slugify(subject.name)}-{date.today():%Y%m%d}"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(f"{name}/pack.html", render_to_string("compliance/pack.html", context, request=request))
            archive.writestr(f"{name}/evidence.json", json.dumps(pack_json(context), indent=2, default=str))
            audit = io.StringIO()
            writer = csv.writer(audit)
            writer.writerow(["when", "actor", "action", "resource", "old_value", "new_value"])
            for entry in context["audit"]:
                writer.writerow([entry.created_at.isoformat(), entry.actor_label or str(entry.actor or ""),
                                 entry.action, entry.resource_label, json.dumps(entry.old_value, default=str),
                                 json.dumps(entry.new_value, default=str)])
            archive.writestr(f"{name}/audit-trail.csv", audit.getvalue())
        response = HttpResponse(buffer.getvalue(), content_type="application/zip")
        response["Content-Disposition"] = f'attachment; filename="{name}.zip"'
        return response
