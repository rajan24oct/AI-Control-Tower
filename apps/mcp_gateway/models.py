"""MCP Gateway state: the registry of MCP servers, and the traffic through it.

Two halves. The *registry* -- servers, the tools they advertise, and which
agents may reach them -- is what an enterprise manages in one place. The
*gateway* serves agents from that registry: `tools/list` is answered from
here, never live from upstream, and only `tools/call` goes out to a server.

That split is the point of the module. A dead server cannot slow down every
agent's session start; a tool is visible to an agent only after someone has
reviewed it; and an upstream cannot quietly change what a reviewed tool does,
because a changed definition goes back to review.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

from apps.core.constants import DataClassification, HealthStatus, RiskLevel
from apps.core.models import OrganizationOwnedModel, OrganizationScopedModel
from apps.gateway.models import CircuitState
from integrations.base.config import AdapterConfig
from integrations.registry import AdapterKind, registry

#: Joins a server's slug to a tool's name in the one namespace an agent sees,
#: e.g. `servicenow__create_incident`. Slugs may not contain it.
SEPARATOR = "__"


class MCPTransport(models.TextChoices):
    """How a server is reached. The value doubles as the adapter key.

    Only transports with a registered adapter can be reached through the
    gateway. The rest are registered for inventory and governance only: a
    stdio server is a process on someone's machine, and the platform will not
    spawn processes on behalf of a web request.
    """

    STREAMABLE_HTTP = "streamable_http", "Streamable HTTP"
    SSE = "sse", "HTTP + SSE (legacy)"
    STDIO = "stdio", "stdio (local process)"
    MOCK = "mock", "Mock / Demo"


class MCPAuthMethod(models.TextChoices):
    NONE = "none", "None"
    BEARER = "bearer", "Bearer token"
    HEADER = "header", "API key header"


class AccessMode(models.TextChoices):
    OPEN = "open", "Any agent in the organization"
    RESTRICTED = "restricted", "Only agents with a grant"


class MCPServer(OrganizationScopedModel):
    """An MCP server the organization's agents may use.

    Credentials are never stored here. As with providers, `credential_ref`
    names an environment variable the gateway resolves at call time -- so an
    agent reaching a tool through the gateway never holds the credential.
    """

    name = models.CharField(max_length=200)
    slug = models.SlugField(
        max_length=200, blank=True,
        help_text="Prefix of this server's tools in the gateway's unified namespace.",
    )
    description = models.TextField(blank=True)
    publisher = models.CharField(
        max_length=200, blank=True, help_text="Who builds and maintains the server."
    )
    category = models.CharField(max_length=100, blank=True)
    documentation_url = models.URLField(max_length=500, blank=True)

    transport = models.CharField(
        max_length=40, choices=MCPTransport.choices, default=MCPTransport.STREAMABLE_HTTP,
        db_index=True,
    )
    endpoint = models.URLField(max_length=500, blank=True)
    command = models.CharField(
        max_length=500, blank=True,
        help_text="For stdio servers: the launch command, recorded for inventory only.",
    )

    auth_method = models.CharField(
        max_length=20, choices=MCPAuthMethod.choices, default=MCPAuthMethod.NONE
    )
    credential_ref = models.CharField(
        max_length=200, blank=True,
        help_text=(
            "Name of the environment variable or secret-manager key holding the "
            "credential. Never paste the credential itself."
        ),
    )
    auth_header = models.CharField(
        max_length=100, blank=True,
        help_text="Header carrying the credential for 'API key header' auth. Defaults to X-API-Key.",
    )
    options = models.JSONField(
        default=dict, blank=True,
        help_text="Adapter-specific settings (non-secret), e.g. static headers or a timeout.",
    )

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="owned_mcp_servers",
    )
    team = models.ForeignKey(
        "organizations.Team", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="mcp_servers",
    )

    data_classification = models.CharField(
        max_length=20, choices=DataClassification.choices, default=DataClassification.INTERNAL,
        help_text="The most sensitive data this server's tools can read or change.",
    )
    risk_level = models.CharField(
        max_length=20, choices=RiskLevel.choices, default=RiskLevel.MODERATE, db_index=True
    )
    is_approved = models.BooleanField(
        default=False,
        help_text="Approved by governance. An unapproved server is invisible to every agent.",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="approved_mcp_servers",
    )
    # Restricted by default: a newly registered server reaching every agent in
    # the estate should be a decision somebody made, not an accident.
    access_mode = models.CharField(
        max_length=20, choices=AccessMode.choices, default=AccessMode.RESTRICTED
    )

    # What the server said about itself at the last discovery.
    server_info = models.JSONField(default=dict, blank=True)
    protocol_version = models.CharField(max_length=20, blank=True)
    advertised_capabilities = models.JSONField(default=dict, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_sync_error = models.CharField(max_length=500, blank=True)

    # Health, from explicit probes.
    health_status = models.CharField(
        max_length=20, choices=HealthStatus.choices, default=HealthStatus.UNKNOWN, db_index=True
    )
    last_health_check = models.DateTimeField(null=True, blank=True)
    last_health_latency_ms = models.PositiveIntegerField(null=True, blank=True)
    last_health_message = models.CharField(max_length=500, blank=True)

    # The breaker, from live traffic. Written on state changes, not every call.
    circuit_state = models.CharField(
        max_length=20, choices=CircuitState.choices, default=CircuitState.CLOSED, db_index=True
    )
    consecutive_failures = models.PositiveIntegerField(default=0)
    circuit_opened_at = models.DateTimeField(null=True, blank=True)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)
    circuit_trips = models.PositiveIntegerField(
        default=0, help_text="Times this server's breaker has opened, ever."
    )

    class Meta:
        ordering = ["name"]
        verbose_name = "MCP server"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "slug"], name="uniq_mcp_server_slug_per_org"
            )
        ]
        indexes = [
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["organization", "is_approved"]),
        ]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        self.slug = self.clean_slug(self.slug or self.name)
        super().save(*args, **kwargs)

    @staticmethod
    def clean_slug(value: str) -> str:
        slug = slugify(value)[:200]
        while SEPARATOR in slug:
            slug = slug.replace(SEPARATOR, "-")
        return slug.strip("-_") or "server"

    def clean(self):
        errors = {}
        if self.transport in {MCPTransport.STREAMABLE_HTTP, MCPTransport.SSE} and not self.endpoint:
            errors["endpoint"] = "An HTTP transport needs an endpoint."
        if self.auth_method != MCPAuthMethod.NONE and not self.credential_ref:
            errors["credential_ref"] = "Name the environment variable that holds the credential."
        if errors:
            raise ValidationError(errors)

    # -- Adapter binding ----------------------------------------------------

    def adapter_config(self, *, timeout: float | None = None) -> AdapterConfig:
        options = dict(self.options or {})
        options.update({"auth_method": self.auth_method, "auth_header": self.auth_header})
        return AdapterConfig(
            key=self.transport,
            name=self.name,
            endpoint=self.endpoint,
            credential_ref=self.credential_ref if self.needs_credential else "",
            timeout=float(timeout if timeout is not None else options.get("timeout", 30.0)),
            options=options,
        )

    def get_adapter(self, *, timeout: float | None = None):
        """Resolve through the registry. Nothing here branches on transport."""
        return registry.get_mcp(self.transport, self.adapter_config(timeout=timeout))

    @property
    def is_gateway_reachable(self) -> bool:
        return registry.has(AdapterKind.MCP, self.transport)

    @property
    def needs_credential(self) -> bool:
        return self.auth_method != MCPAuthMethod.NONE

    @property
    def has_resolvable_credential(self) -> bool:
        return not self.needs_credential or self.adapter_config().has_credential()

    @property
    def is_serving(self) -> bool:
        """Visible to agents at all, before any per-agent or per-tool check."""
        return self.is_active and self.is_approved and self.is_gateway_reachable

    @property
    def has_incident(self) -> bool:
        return self.circuit_state == CircuitState.OPEN or self.health_status in {
            HealthStatus.DEGRADED,
            HealthStatus.UNHEALTHY,
        }

    def circuit_available(self, *, cool_off_seconds: int) -> bool:
        """Whether a call may be attempted now.

        An open breaker lets one probe through after the cool-off, so recovery
        is found by traffic rather than by a timer nobody is watching.
        """
        if self.circuit_state != CircuitState.OPEN:
            return True
        return bool(
            self.circuit_opened_at
            and timezone.now() - self.circuit_opened_at >= timedelta(seconds=cool_off_seconds)
        )


class ToolStatus(models.TextChoices):
    PENDING_REVIEW = "pending_review", "Awaiting review"
    ENABLED = "enabled", "Enabled"
    DISABLED = "disabled", "Disabled"
    REMOVED = "removed", "Removed upstream"


class MCPTool(OrganizationOwnedModel):
    """A tool one server advertises, as governance last saw it."""

    server = models.ForeignKey(MCPServer, on_delete=models.CASCADE, related_name="tools")
    name = models.CharField(max_length=128)
    title = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    input_schema = models.JSONField(default=dict, blank=True)
    output_schema = models.JSONField(null=True, blank=True)
    annotations = models.JSONField(default=dict, blank=True)
    definition_hash = models.CharField(max_length=64, blank=True)

    status = models.CharField(
        max_length=20, choices=ToolStatus.choices, default=ToolStatus.PENDING_REVIEW,
        db_index=True,
    )
    risk_level = models.CharField(
        max_length=20, choices=RiskLevel.choices, default=RiskLevel.MODERATE, db_index=True
    )
    changed_since_review = models.BooleanField(
        default=False,
        help_text="The server changed this tool's definition after it was last reviewed.",
    )
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="reviewed_mcp_tools",
    )
    review_note = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["server__name", "name"]
        verbose_name = "MCP tool"
        constraints = [
            models.UniqueConstraint(fields=["server", "name"], name="uniq_mcp_tool_per_server")
        ]
        indexes = [models.Index(fields=["organization", "status"])]

    def __str__(self) -> str:
        return self.qualified_name

    @property
    def qualified_name(self) -> str:
        return f"{self.server.slug}{SEPARATOR}{self.name}"

    @property
    def is_read_only(self) -> bool:
        return self.annotations.get("readOnlyHint") is True

    @property
    def is_destructive(self) -> bool:
        # Per the MCP spec, a tool that writes is assumed destructive unless it
        # says otherwise -- the hint defaults to true, not false.
        if self.annotations.get("destructiveHint") is True:
            return True
        return (
            self.annotations.get("readOnlyHint") is False
            and self.annotations.get("destructiveHint") is not False
        )

    @property
    def is_callable(self) -> bool:
        return self.status == ToolStatus.ENABLED


class MCPAccessGrant(OrganizationOwnedModel):
    """Lets one agent reach a restricted server, optionally only some tools."""

    server = models.ForeignKey(MCPServer, on_delete=models.CASCADE, related_name="grants")
    agent = models.ForeignKey(
        "agents.AIAgent", on_delete=models.CASCADE, related_name="mcp_grants"
    )
    allowed_tools = models.JSONField(
        default=list, blank=True,
        help_text="Tool names as the server names them. Empty means every enabled tool.",
    )
    expires_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["server__name", "agent__name"]
        verbose_name = "MCP access grant"
        constraints = [
            models.UniqueConstraint(fields=["server", "agent"], name="uniq_mcp_grant_per_agent")
        ]

    def __str__(self) -> str:
        return f"{self.agent} -> {self.server}"

    def clean(self):
        if self.server_id and self.agent_id and (
            self.agent.organization_id != self.server.organization_id
        ):
            raise ValidationError("The agent and the server must belong to the same organization.")

    def save(self, *args, **kwargs):
        # A grant lives wherever its server lives; it is never chosen separately.
        self.organization_id = self.server.organization_id
        super().save(*args, **kwargs)

    @property
    def is_expired(self) -> bool:
        return bool(self.expires_at and self.expires_at <= timezone.now())

    def permits(self, tool_name: str) -> bool:
        return not self.allowed_tools or tool_name in self.allowed_tools


class CallStatus(models.TextChoices):
    SUCCEEDED = "succeeded", "Succeeded"
    TOOL_ERROR = "tool_error", "Tool reported an error"
    BLOCKED = "blocked", "Blocked"
    UNAVAILABLE = "unavailable", "Server unavailable"
    FAILED = "failed", "Upstream failed"


class CallChannel(models.TextChoices):
    MCP = "mcp", "MCP endpoint"
    REST = "rest", "REST API"


class MCPToolCall(OrganizationOwnedModel):
    """One tool call brokered by the gateway -- the refused ones included.

    Its own record rather than an Execution: a tool call is a step inside a run,
    not a run, and counting each one as an execution would inflate every run
    metric on the platform. When the caller names the run it belongs to, the
    call is also written onto that run's timeline.
    """

    server = models.ForeignKey(
        MCPServer, on_delete=models.SET_NULL, null=True, blank=True, related_name="calls"
    )
    tool = models.ForeignKey(
        MCPTool, on_delete=models.SET_NULL, null=True, blank=True, related_name="calls"
    )
    tool_name = models.CharField(max_length=300, help_text="The name as the caller used it.")
    agent = models.ForeignKey(
        "agents.AIAgent", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="mcp_tool_calls",
    )
    service_account = models.ForeignKey(
        "api.ServiceAccount", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="mcp_tool_calls",
    )
    execution = models.ForeignKey(
        "executions.Execution", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="mcp_tool_calls",
    )
    channel = models.CharField(max_length=10, choices=CallChannel.choices, default=CallChannel.MCP)

    status = models.CharField(max_length=20, choices=CallStatus.choices, db_index=True)
    reason = models.CharField(max_length=1000, blank=True)
    policies = models.JSONField(default=list, blank=True)
    latency_ms = models.PositiveIntegerField(null=True, blank=True)
    arguments = models.JSONField(default=dict, blank=True)
    result_preview = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "MCP tool call"
        indexes = [
            models.Index(fields=["organization", "-created_at"]),
            models.Index(fields=["organization", "status", "-created_at"]),
            models.Index(fields=["server", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.tool_name} ({self.get_status_display()})"

    @property
    def reached_server(self) -> bool:
        return self.status in {CallStatus.SUCCEEDED, CallStatus.TOOL_ERROR}
