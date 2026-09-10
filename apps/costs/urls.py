from django.urls import path

from apps.costs import views

app_name = "costs"

urlpatterns = [
    path("", views.CostDashboardView.as_view(), name="dashboard"),
    path("budgets/", views.BudgetListView.as_view(), name="budgets"),
    path("alerts/<uuid:pk>/acknowledge/", views.AcknowledgeAlertView.as_view(), name="acknowledge_alert"),
]
