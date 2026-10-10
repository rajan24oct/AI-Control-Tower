from django.apps import AppConfig


class ComplianceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.compliance"
    verbose_name = "Compliance and evidence (EU AI Act, ISO/IEC 42001, NIST AI RMF)"
