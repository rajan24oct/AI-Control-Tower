from django.urls import path

from apps.costs import manage, views

app_name = "costs"

urlpatterns = [
    path("", views.CostDashboardView.as_view(), name="dashboard"),
    path("explorer/", views.CostExplorerView.as_view(), name="explorer"),
    path("explorer/data/", views.CostExplorerDataView.as_view(), name="explorer_data"),
    path("developer-tools/", views.DeveloperToolsView.as_view(), name="developer_tools"),
    path("developer-tools/new/", manage.DevToolSubscriptionFormView.as_view(), name="subscription_create"),
    path("developer-tools/<uuid:pk>/edit/", manage.DevToolSubscriptionFormView.as_view(), name="subscription_edit"),
    path("developer-tools/<uuid:pk>/sync/", views.SyncSubscriptionView.as_view(), name="subscription_sync"),
    path("developer-tools/seats/<uuid:pk>/edit/", manage.DevToolSeatFormView.as_view(), name="seat_edit"),
    path("budgets/", views.BudgetListView.as_view(), name="budgets"),
    path("budgets/new/", manage.BudgetFormView.as_view(), name="budget_create"),
    path("budgets/<uuid:pk>/", views.BudgetDetailView.as_view(), name="budget_detail"),
    path("budgets/<uuid:pk>/edit/", manage.BudgetFormView.as_view(), name="budget_edit"),
    path("alerts/<uuid:pk>/acknowledge/", views.AcknowledgeAlertView.as_view(), name="acknowledge_alert"),
    path("governance/", views.GovernanceView.as_view(), name="governance"),
    path("governance/evaluate/", views.EvaluateNowView.as_view(), name="evaluate_now"),
    path("governance/rules/new/", manage.CostAlertRuleFormView.as_view(), name="rule_create"),
    path("governance/rules/<uuid:pk>/edit/", manage.CostAlertRuleFormView.as_view(), name="rule_edit"),
    path("governance/events/<uuid:pk>/acknowledge/", views.AcknowledgeEventView.as_view(), name="acknowledge_event"),
    path("optimization/", views.OptimizationView.as_view(), name="optimization"),
    path("optimization/run/", views.RunOptimizationView.as_view(), name="optimization_run"),
    path("optimization/<uuid:pk>/decide/", views.RecommendationDecisionView.as_view(), name="recommendation_decide"),
]
