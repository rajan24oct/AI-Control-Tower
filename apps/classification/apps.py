from django.apps import AppConfig


class ClassificationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.classification"
    verbose_name = "Risk classification (IRGF S2)"

    def ready(self):
        # Re-score triggers: autonomy and data-sensitivity increases observed
        # on the registry flag the current classification.
        from apps.classification import signals  # noqa: F401
