"""Role definitions and the RBAC permission matrix (section 28).

Permissions are plain strings rather than Django model permissions because
Control Tower authorization is organization-scoped: the same user can be an
Approver in one organization and a Viewer in another.
"""

from __future__ import annotations

from django.db import models


class Role(models.TextChoices):
    SUPER_ADMIN = "super_admin", "Super Administrator"
    ORG_ADMIN = "org_admin", "Organization Administrator"
    PLATFORM_ADMIN = "platform_admin", "AI Platform Administrator"
    AI_DEVELOPER = "ai_developer", "AI Developer"
    GOVERNANCE_MANAGER = "governance_manager", "AI Governance Manager"
    RISK_MANAGER = "risk_manager", "Risk Manager"
    APPROVER = "approver", "Approver"
    AUDITOR = "auditor", "Auditor"
    VIEWER = "viewer", "Viewer"


class Perm:
    """Capability vocabulary checked throughout the platform."""

    ORG_MANAGE = "org.manage"
    USER_MANAGE = "user.manage"

    INVENTORY_VIEW = "inventory.view"
    INVENTORY_MANAGE = "inventory.manage"

    PROVIDER_MANAGE = "provider.manage"
    MODEL_MANAGE = "model.manage"
    MODEL_APPROVE = "model.approve"
    AGENT_MANAGE = "agent.manage"
    APPLICATION_MANAGE = "application.manage"

    EXECUTION_VIEW = "execution.view"
    TELEMETRY_INGEST = "telemetry.ingest"
    # Separate from EXECUTION_VIEW: prompt and completion text is the one place
    # customer data reliably shows up, so watching operations and reading what
    # was actually said are different privileges.
    PROMPT_VIEW = "prompt.view"

    COST_VIEW = "cost.view"
    COST_MANAGE = "cost.manage"

    RISK_VIEW = "risk.view"
    RISK_MANAGE = "risk.manage"

    APPROVAL_VIEW = "approval.view"
    APPROVAL_DECIDE = "approval.decide"

    POLICY_VIEW = "policy.view"
    POLICY_MANAGE = "policy.manage"

    AUDIT_VIEW = "audit.view"


_READ_ONLY = {
    Perm.INVENTORY_VIEW,
    Perm.EXECUTION_VIEW,
    Perm.COST_VIEW,
    Perm.RISK_VIEW,
    Perm.APPROVAL_VIEW,
    Perm.POLICY_VIEW,
}

_ALL_PERMISSIONS = {
    value
    for name, value in vars(Perm).items()
    if not name.startswith("_") and isinstance(value, str)
}

# SUPER_ADMIN and ORG_ADMIN hold every permission; they differ in scope, not in
# capability -- a super administrator spans organizations, an organization
# administrator is confined to their own.
ROLE_PERMISSIONS: dict[str, set[str]] = {
    Role.SUPER_ADMIN: set(_ALL_PERMISSIONS),
    Role.ORG_ADMIN: set(_ALL_PERMISSIONS),
    Role.PLATFORM_ADMIN: _READ_ONLY
    | {
        Perm.INVENTORY_MANAGE,
        Perm.PROVIDER_MANAGE,
        Perm.MODEL_MANAGE,
        Perm.AGENT_MANAGE,
        Perm.APPLICATION_MANAGE,
        Perm.TELEMETRY_INGEST,
        Perm.COST_MANAGE,
        Perm.PROMPT_VIEW,
    },
    Role.AI_DEVELOPER: _READ_ONLY
    | {
        Perm.INVENTORY_MANAGE,
        Perm.AGENT_MANAGE,
        Perm.APPLICATION_MANAGE,
        Perm.TELEMETRY_INGEST,
        Perm.PROMPT_VIEW,
    },
    Role.GOVERNANCE_MANAGER: _READ_ONLY
    | {
        Perm.MODEL_APPROVE,
        Perm.POLICY_MANAGE,
        Perm.RISK_MANAGE,
        Perm.APPROVAL_DECIDE,
        Perm.AUDIT_VIEW,
        Perm.PROMPT_VIEW,
    },
    Role.RISK_MANAGER: _READ_ONLY | {Perm.RISK_MANAGE, Perm.AUDIT_VIEW, Perm.PROMPT_VIEW},
    # An approver deciding blind is worse than no approval at all.
    Role.APPROVER: _READ_ONLY | {Perm.APPROVAL_DECIDE, Perm.PROMPT_VIEW},
    Role.AUDITOR: _READ_ONLY | {Perm.AUDIT_VIEW, Perm.PROMPT_VIEW},
    # Viewer is deliberately the one role that can watch operations without
    # reading content -- the role to hand to a broad internal audience.
    Role.VIEWER: set(_READ_ONLY),
}


def role_permissions(role: str) -> set[str]:
    return ROLE_PERMISSIONS.get(role, set())


def role_has_permission(role: str, permission: str) -> bool:
    return permission in role_permissions(role)
