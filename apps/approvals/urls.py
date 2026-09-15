from django.urls import path

from apps.approvals import manage, views

app_name = "approvals"

urlpatterns = [
    path("", views.ApprovalCentreView.as_view(), name="centre"),
    path("rules/new/", manage.ApprovalRuleFormView.as_view(), name="rule_create"),
    path("rules/<uuid:pk>/edit/", manage.ApprovalRuleFormView.as_view(), name="rule_edit"),
    path("<uuid:pk>/", views.ApprovalDetailView.as_view(), name="detail"),
    path("<uuid:pk>/<str:decision>/", views.ApprovalDecisionView.as_view(), name="decide"),
]
