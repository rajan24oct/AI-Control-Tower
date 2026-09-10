"""Verify that no audit entry has been altered since it was written."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.audit.models import AuditLog


class Command(BaseCommand):
    help = "Recompute every audit entry's hash and report any that no longer match."

    def add_arguments(self, parser):
        parser.add_argument(
            "--organization", default="", help="Limit the check to one organization name."
        )
        parser.add_argument(
            "--fail-on-tamper",
            action="store_true",
            help="Exit non-zero when a modified entry is found, for use in CI or a cron check.",
        )

    def handle(self, *args, **options):
        queryset = AuditLog.objects.all().order_by("created_at")
        if options["organization"]:
            queryset = queryset.filter(organization__name=options["organization"])

        checked = verified = legacy = 0
        tampered = []

        for entry in queryset.iterator():
            checked += 1
            if not entry.is_verifiable:
                legacy += 1
                continue
            if entry.verify():
                verified += 1
            else:
                tampered.append(entry)

        self.stdout.write(f"Checked {checked} entr{'y' if checked == 1 else 'ies'}.")
        self.stdout.write(self.style.SUCCESS(f"  {verified} verified"))
        if legacy:
            self.stdout.write(
                self.style.WARNING(f"  {legacy} written before hashing (cannot be verified)")
            )

        if tampered:
            self.stdout.write(self.style.ERROR(f"  {len(tampered)} MODIFIED since written:"))
            for entry in tampered[:25]:
                self.stdout.write(
                    self.style.ERROR(
                        f"    {entry.created_at:%Y-%m-%d %H:%M:%S}  {entry.id}  "
                        f"{entry.action} {entry.resource_type}"
                    )
                )
            if options["fail_on_tamper"]:
                raise SystemExit(1)
        else:
            self.stdout.write(self.style.SUCCESS("  No modified entries found."))
