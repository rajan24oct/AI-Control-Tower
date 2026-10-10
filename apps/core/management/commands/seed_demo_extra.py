"""Add a deeper layer of FinOps and governance demo data on top of seed_demo."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.controlplane.signals import deferred
from apps.core.demo_extended import seed_extended
from apps.core.management.commands.seed_demo import ORGANIZATION_NAME
from apps.organizations.models import Organization


class Command(BaseCommand):
    help = (
        "Add ninety days of AI spend, more Copilot seats, budgets, alert rules, risks, approvals, "
        "policy checks and evaluation runs to the demo organization. Run seed_demo first; safe to rerun."
    )

    def handle(self, *args, **options):
        organization = Organization.objects.filter(name=ORGANIZATION_NAME).first()
        if organization is None:
            raise CommandError(f"'{ORGANIZATION_NAME}' does not exist. Run `manage.py seed_demo` first.")
        users = {
            user.email: user
            for user in get_user_model().objects.filter(organization_memberships__organization=organization)
        }
        self.stdout.write(f"Extending '{organization.name}':")
        with deferred():
            result = seed_extended(organization, users, say=self.stdout.write)

        self.stdout.write(self.style.SUCCESS("Done"))
        self.stdout.write(
            f"  {result['executions']:,} historical runs over 90 days, {result['seats']} more Copilot seats\n"
            f"  {result['budgets']} more budgets ({result['budget_alerts']} alerts, earlier months acknowledged), "
            f"{result['rules']} more alert rules ({result['rule_events']} firings over the last month)\n"
            f"  {result['risks']} more risks, {result['approvals']} more approval decisions, "
            f"{result['policy_findings']} policy findings, {result['evaluation_runs']} evaluation runs\n"
            f"  {result['recommendations']} optimization recommendations, {result['use_cases']} use cases at Gate G1"
        )
