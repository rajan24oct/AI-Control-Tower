"""Celery application for the AI Control Tower."""

from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "aig_tower.settings")

app = Celery("aig_tower")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Periodic jobs (section 24). Tasks are registered by their owning app; the
# schedule lives here so the operational cadence is visible in one place.
app.conf.beat_schedule = {
    "provider-health-checks": {
        "task": "apps.providers.tasks.run_provider_health_checks",
        "schedule": 300.0,
    },
    "budget-threshold-checks": {
        "task": "apps.costs.tasks.check_budgets",
        "schedule": 900.0,
    },
    "daily-usage-rollups": {
        "task": "apps.costs.tasks.rebuild_daily_rollups",
        "schedule": 3600.0,
    },
    "expire-stale-approvals": {
        "task": "apps.approvals.tasks.expire_stale_approvals",
        "schedule": 600.0,
    },
    "notify-expiring-approvals": {
        "task": "apps.notifications.tasks.notify_expiring_approvals",
        "schedule": 1800.0,
    },
    "notify-overdue-risk-reviews": {
        "task": "apps.notifications.tasks.notify_overdue_risk_reviews",
        "schedule": 86400.0,
    },
    # IRGF M17 and G3: expire exceptions, warn before they lapse, and escalate
    # authorization conditions past their deadline.
    "lifecycle-sweep": {
        "task": "apps.lifecycle.tasks.sweep_lifecycle",
        "schedule": 3600.0,
    },
    # IRGF M12: verify actions the post-save hook missed (link 07), observe
    # evaluations, policies, budgets and tool definitions, escalate overdue drift.
    "control-plane-sweep": {
        "task": "apps.controlplane.tasks.sweep_control_plane",
        "schedule": 60.0,
    },
}
