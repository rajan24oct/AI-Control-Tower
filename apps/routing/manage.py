"""Creating and editing routing policies in the web UI."""

from __future__ import annotations

from apps.accounts.roles import Perm
from apps.core.manage import ManagedFormView
from apps.routing.forms import RoutingPolicyForm
from apps.routing.models import STRATEGY_WEIGHTS, RoutingPolicy, RoutingStrategy


class RoutingPolicyFormView(ManagedFormView):
    model = RoutingPolicy
    form_class = RoutingPolicyForm
    # Which models and providers may serve traffic is the platform team's call,
    # alongside registering the providers themselves.
    permission = Perm.PROVIDER_MANAGE
    denied_message = "Routing policies are managed by holders of provider.manage."
    noun = "routing policy"
    create_heading = "New routing policy"
    create_lead = "Which models may serve a class of work, in what order, and how many fallbacks the gateway tries."
    create_submit_label = "Create routing policy"
    list_url_name = "routing:list"
    list_label = "Model routing"
    detail_url_name = "routing:detail"
    guide_template = "manage/guides/_routing.html"

    def get_context_data(self):
        return {
            "strategy_weights": [
                (RoutingStrategy(key).label, weights) for key, weights in STRATEGY_WEIGHTS.items()
            ],
        }

    def success_message(self, obj, creating, changed):
        if creating:
            return f"Routing policy “{obj}” created. Its page shows the plan it would produce right now."
        return super().success_message(obj, creating, changed)
