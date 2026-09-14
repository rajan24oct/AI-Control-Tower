from django.apps import AppConfig


class ControlPlaneConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.controlplane"
    verbose_name = "IRGF Control Plane (M12), drift (M13) and verification (link 07)"

    def ready(self):
        # Verification: every recorded tool call is checked against the authorized state.
        from apps.controlplane import signals  # noqa: F401
