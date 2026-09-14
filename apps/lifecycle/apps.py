from django.apps import AppConfig


class LifecycleConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.lifecycle"
    verbose_name = "IRGF lifecycle: authorization (G3), change (G4) and exceptions (M17)"

    def ready(self):
        # Change detection: edits to an authorized agent become change records.
        from apps.lifecycle import signals  # noqa: F401
