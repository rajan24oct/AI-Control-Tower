from django.urls import path

from apps.costs import manage, views

app_name = "costs"

urlpatterns = [
    path("", views.CostDashboardView.as_view(), name="dashboard"),
    path("budgets/", views.BudgetListView.as_view(), name="budgets"),
    path("budgets/new/", manage.BudgetFormView.as_view(), name="budget_create"),
    path("budgets/<uuid:pk>/edit/", manage.BudgetFormView.as_view(), name="budget_edit"),
    path("alerts/<uuid:pk>/acknowledge/", views.AcknowledgeAlertView.as_view(), name="acknowledge_alert"),
]
