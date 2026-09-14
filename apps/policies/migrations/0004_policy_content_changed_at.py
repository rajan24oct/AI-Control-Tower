from django.db import migrations, models
from django.db.models import F


def backfill(apps, schema_editor):
    # What a policy did before the field existed is known only from when it was created.
    Policy = apps.get_model("policies", "Policy")
    Policy.objects.update(content_changed_at=F("created_at"))


class Migration(migrations.Migration):

    dependencies = [
        ("policies", "0003_alter_policy_policy_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="policy",
            name="content_changed_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
