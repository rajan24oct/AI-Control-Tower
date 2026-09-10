from django.apps import AppConfig


class IntegrationsConfig(AppConfig):
    name = "apps.integrations"
    label = "integrations"
    verbose_name = "Integrations & Adapters"

    def ready(self):
        # Import adapter modules so their registry decorators run exactly once,
        # after the app registry is populated.
        from integrations.bootstrap import load_adapters

        load_adapters()
