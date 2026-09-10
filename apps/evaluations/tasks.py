"""Background execution of evaluation runs.

A suite can be dozens of real model calls, which is minutes -- far too long to
hold a web request open. The run is created synchronously so the user gets a
page to watch, and the work happens here.
"""

from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="apps.evaluations.tasks.execute_run")
def execute_run(run_id: str) -> str:
    """Run a suite end to end. Safe to lose: a queued run can be re-queued."""
    from apps.evaluations.models import EvaluationRun
    from apps.evaluations.services import EvaluationService

    run = EvaluationRun.objects.filter(pk=run_id).select_related("suite", "model", "agent").first()
    if run is None:
        logger.warning("Evaluation run %s no longer exists", run_id)
        return "missing"

    EvaluationService.execute(run)
    return run.status
