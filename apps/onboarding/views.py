"""The Onboarding hub: one page that walks an organization from empty to governed."""

from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView

from apps.onboarding.services import DONE, OPTIONAL, checklist, create_links


class OnboardingHubView(LoginRequiredMixin, TemplateView):
    template_name = "onboarding/hub.html"
    extra_context = {
        "page_title": "Onboarding",
        "page_subtitle": "Bring providers, models, agents and applications under governance, in order",
    }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        organization = self.request.organization
        if organization is None:
            return context

        def allowed(permission: str) -> bool:
            return self.request.user.has_org_permission(organization, permission)

        steps = checklist(organization, allowed)
        context.update({
            "steps": steps,
            "done": sum(1 for step in steps if step.state == DONE),
            "required": sum(1 for step in steps if step.state != OPTIONAL),
            "create_links": create_links(allowed),
        })
        return context
