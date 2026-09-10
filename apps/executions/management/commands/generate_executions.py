"""Generate mock execution telemetry for an organization."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.executions.generators import MockExecutionGenerator, purge
from apps.organizations.models import Organization


class Command(BaseCommand):
    help = "Generate realistic mock executions and events for demos and testing."

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=500, help="Executions to create.")
        parser.add_argument("--days", type=int, default=14, help="Days of history to span.")
        parser.add_argument("--organization", default="", help="Organization name or slug.")
        parser.add_argument(
            "--purge", action="store_true", help="Delete existing mock executions first."
        )
        parser.add_argument("--seed", type=int, default=20260909)

    def handle(self, *args, **options):
        organization = self._organization(options["organization"])

        if options["purge"]:
            removed = purge(organization)
            if options["verbosity"]:
                self.stdout.write(f"Removed {removed} existing mock execution(s).")

        generator = MockExecutionGenerator(
            organization, days=options["days"], seed=options["seed"]
        )
        if not generator.agents:
            raise CommandError(
                f"'{organization.name}' has no agents. Run `manage.py seed_demo` first."
            )

        result = generator.generate(options["count"])

        if options["verbosity"]:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Created {result.executions} execution(s) and {result.events} event(s) "
                    f"for '{organization.name}'."
                )
            )

    @staticmethod
    def _organization(identifier: str) -> Organization:
        if identifier:
            organization = Organization.objects.filter(name=identifier).first() or (
                Organization.objects.filter(slug=identifier).first()
            )
            if organization is None:
                raise CommandError(f"No organization matching '{identifier}'.")
            return organization

        organization = Organization.objects.first()
        if organization is None:
            raise CommandError("No organizations exist. Run `manage.py seed_demo` first.")
        return organization
