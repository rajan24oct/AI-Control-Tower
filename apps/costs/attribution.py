"""Who a run's cost belongs to.

Executions record the application, agent, model, provider and (when known)
the person who caused them -- but not a department or a team. Those come from
the registry, and the same run can reach a team two ways: through the
application it served or through the agent that ran it. This module is the one
place that decides, so the cost explorer, budgets and alert rules all attribute
a run to the same department and team.

Precedence, for both: the application first, then the agent. An application
is what the business owns and budgets for; the agent is the fallback for runs
that served no registered application.
"""

from __future__ import annotations

from django.db.models import CharField, F, Q, Value
from django.db.models.functions import Coalesce, NullIf

from apps.costs.models import BudgetScope

UNASSIGNED = "__none__"


def _blank_is_null(field: str):
    return NullIf(F(field), Value(""))


def department_expression():
    return Coalesce(
        _blank_is_null("application__department"),
        _blank_is_null("application__team__department"),
        _blank_is_null("agent__team__department"),
        output_field=CharField(),
    )


def team_id_expression():
    return Coalesce(F("application__team_id"), F("agent__team_id"))


def team_name_expression():
    return Coalesce(F("application__team__name"), F("agent__team__name"), output_field=CharField())


def with_attribution(queryset):
    """Annotate executions with their attributed department and team."""
    return queryset.annotate(
        attributed_department=department_expression(),
        attributed_team_id=team_id_expression(),
    )


#: Scopes whose target is a plain foreign key on the execution.
DIRECT_SCOPES = {
    BudgetScope.APPLICATION: "application",
    BudgetScope.AGENT: "agent",
    BudgetScope.PROVIDER: "provider",
    BudgetScope.MODEL: "model",
    BudgetScope.USER: "user",
}


def scope_executions(queryset, holder):
    """Narrow executions to what a budget or alert rule covers.

    `holder` is anything with the budget scope fields (`scope`, `department`,
    `team_id`, `user_id`, ...). A scoped holder whose target is missing
    matches nothing rather than everything: a half-configured budget must not
    silently count the whole organization.
    """
    scope = holder.scope
    if scope == BudgetScope.ORGANIZATION:
        return queryset

    if scope == BudgetScope.DEPARTMENT:
        department = (holder.department or "").strip()
        if not department:
            return queryset.none()
        return with_attribution(queryset).filter(attributed_department__iexact=department)

    if scope == BudgetScope.TEAM:
        if not holder.team_id:
            return queryset.none()
        return with_attribution(queryset).filter(attributed_team_id=holder.team_id)

    field = DIRECT_SCOPES.get(scope)
    if field:
        target_id = getattr(holder, f"{field}_id")
        return queryset.filter(**{f"{field}_id": target_id}) if target_id else queryset.none()

    return queryset.none()


def scope_seats(queryset, holder):
    """Narrow developer-tool seats to what a budget covers.

    Seats belong to people and teams; a budget scoped to an agent, model or
    application has no seats in it.
    """
    scope = holder.scope
    if scope == BudgetScope.ORGANIZATION:
        return queryset
    if scope == BudgetScope.TEAM:
        return queryset.filter(team_id=holder.team_id) if holder.team_id else queryset.none()
    if scope == BudgetScope.USER:
        return queryset.filter(user_id=holder.user_id) if holder.user_id else queryset.none()
    if scope == BudgetScope.DEPARTMENT:
        department = (holder.department or "").strip()
        if not department:
            return queryset.none()
        return queryset.filter(team__department__iexact=department)
    return queryset.none()


def unassigned_filter(field: str) -> Q:
    return Q(**{f"{field}__isnull": True})
