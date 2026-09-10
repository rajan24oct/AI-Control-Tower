"""Vocabulary shared across every Control Tower module.

These enums are deliberately provider-neutral: nothing here names a specific
vendor, framework or SDK.
"""

from __future__ import annotations

from django.db import models


class LifecycleStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    PENDING_REVIEW = "pending_review", "Pending Review"
    ACTIVE = "active", "Active"
    INACTIVE = "inactive", "Inactive"
    DEPRECATED = "deprecated", "Deprecated"
    RETIRED = "retired", "Retired"


class Environment(models.TextChoices):
    SANDBOX = "sandbox", "Sandbox"
    DEVELOPMENT = "development", "Development"
    TEST = "test", "Test"
    STAGING = "staging", "Staging"
    PRODUCTION = "production", "Production"


class Criticality(models.TextChoices):
    LOW = "low", "Low"
    MEDIUM = "medium", "Medium"
    HIGH = "high", "High"
    CRITICAL = "critical", "Critical"


class DataClassification(models.TextChoices):
    PUBLIC = "public", "Public"
    INTERNAL = "internal", "Internal"
    CONFIDENTIAL = "confidential", "Confidential"
    RESTRICTED = "restricted", "Restricted"


class RiskLevel(models.TextChoices):
    LOW = "low", "Low"
    MODERATE = "moderate", "Moderate"
    HIGH = "high", "High"
    CRITICAL = "critical", "Critical"


class HealthStatus(models.TextChoices):
    HEALTHY = "healthy", "Healthy"
    DEGRADED = "degraded", "Degraded"
    UNHEALTHY = "unhealthy", "Unhealthy"
    UNKNOWN = "unknown", "Unknown"


#: Bootstrap contextual class per risk level, for template badges.
RISK_LEVEL_STYLES = {
    RiskLevel.LOW: "success",
    RiskLevel.MODERATE: "warning",
    RiskLevel.HIGH: "danger",
    RiskLevel.CRITICAL: "dark",
}

HEALTH_STATUS_STYLES = {
    HealthStatus.HEALTHY: "success",
    HealthStatus.DEGRADED: "warning",
    HealthStatus.UNHEALTHY: "danger",
    HealthStatus.UNKNOWN: "secondary",
}
