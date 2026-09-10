"""Provider registry (section 11)."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.text import slugify

from apps.core.constants import HealthStatus
from apps.core.models import OrganizationScopedModel
from integrations.base.config import AdapterConfig
from integrations.registry import registry


class ProviderType(models.TextChoices):
    """Taxonomy label. The functional binding is `adapter_key`."""

    OPENAI = "openai", "OpenAI"
    AZURE_OPENAI = "azure_openai", "Azure OpenAI"
    ANTHROPIC = "anthropic", "Anthropic"
    GOOGLE_GEMINI = "google_gemini", "Google Gemini"
    VERTEX_AI = "vertex_ai", "Google Vertex AI"
    AWS_BEDROCK = "aws_bedrock", "AWS Bedrock"
    MISTRAL = "mistral", "Mistral"
    COHERE = "cohere", "Cohere"
    HUGGINGFACE = "huggingface", "Hugging Face"
    OLLAMA = "ollama", "Ollama"
    INTERNAL_ML = "internal_ml", "Internal ML Service"
    CUSTOM_REST = "custom_rest", "Custom REST Service"
    MOCK = "mock", "Mock / Demo"


class AuthenticationMethod(models.TextChoices):
    API_KEY = "api_key", "API Key"
    OAUTH2 = "oauth2", "OAuth 2.0"
    AZURE_AD = "azure_ad", "Microsoft Entra ID"
    IAM_ROLE = "iam_role", "Cloud IAM Role"
    SERVICE_ACCOUNT = "service_account", "Service Account"
    MTLS = "mtls", "Mutual TLS"
    NONE = "none", "None"


class AIProvider(OrganizationScopedModel):
    """An external AI provider the organization consumes.

    Credentials are never stored here. `credential_ref` names an environment
    variable or secret-manager key that the adapter resolves at call time.
    """

    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, blank=True)
    description = models.TextField(blank=True)

    provider_type = models.CharField(
        max_length=40, choices=ProviderType.choices, default=ProviderType.MOCK, db_index=True
    )
    adapter_key = models.CharField(
        max_length=64,
        default="mock",
        help_text="Key of the registered adapter used to reach this provider.",
    )

    region = models.CharField(max_length=100, blank=True)
    endpoint = models.URLField(blank=True)
    deployment = models.CharField(
        max_length=200, blank=True, help_text="Deployment or resource name, where the vendor uses one."
    )

    authentication_method = models.CharField(
        max_length=40, choices=AuthenticationMethod.choices, default=AuthenticationMethod.API_KEY
    )
    credential_ref = models.CharField(
        max_length=200,
        blank=True,
        help_text=(
            "Name of the environment variable or secret-manager key holding the "
            "credential. Never paste the credential itself."
        ),
    )
    options = models.JSONField(
        default=dict, blank=True, help_text="Adapter-specific configuration (non-secret)."
    )

    supported_capabilities = models.JSONField(default=list, blank=True)
    is_approved = models.BooleanField(
        default=False, help_text="Approved for production use by governance."
    )

    health_status = models.CharField(
        max_length=20, choices=HealthStatus.choices, default=HealthStatus.UNKNOWN, db_index=True
    )
    last_health_check = models.DateTimeField(null=True, blank=True)
    last_health_message = models.CharField(max_length=500, blank=True)
    last_health_latency_ms = models.PositiveIntegerField(null=True, blank=True)

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="owned_providers",
    )

    class Meta:
        ordering = ["name"]
        verbose_name = "AI provider"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "slug"], name="uniq_provider_slug_per_org"
            )
        ]
        indexes = [
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["organization", "health_status"]),
            models.Index(fields=["provider_type"]),
        ]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)[:200]
        super().save(*args, **kwargs)

    # -- Adapter binding ----------------------------------------------------

    def adapter_config(self) -> AdapterConfig:
        return AdapterConfig(
            key=self.adapter_key,
            name=self.name,
            endpoint=self.endpoint,
            region=self.region,
            deployment=self.deployment,
            credential_ref=self.credential_ref,
            options=self.options or {},
        )

    def get_adapter(self):
        """Resolve this provider's adapter through the registry.

        Nothing in the platform branches on `provider_type` to pick behaviour.
        """
        return registry.get_provider(self.adapter_key, self.adapter_config())

    @property
    def has_resolvable_credential(self) -> bool:
        return self.adapter_config().has_credential()

    @property
    def is_healthy(self) -> bool:
        return self.health_status == HealthStatus.HEALTHY

    @property
    def has_incident(self) -> bool:
        return self.health_status in {HealthStatus.DEGRADED, HealthStatus.UNHEALTHY}
