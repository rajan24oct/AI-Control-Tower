from django.urls import path

from apps.a2a import manage, views

app_name = "a2a"

urlpatterns = [
    path("", views.A2AOverviewView.as_view(), name="overview"),
    path("agents/", views.CardListView.as_view(), name="agents"),
    path("agents/new/", manage.CardFormView.as_view(), name="card_create"),
    path("agents/<uuid:pk>/", views.CardDetailView.as_view(), name="card_detail"),
    path("agents/<uuid:pk>/edit/", manage.CardFormView.as_view(), name="card_edit"),
    path("agents/<uuid:pk>/sync/", views.SyncCardView.as_view(), name="card_sync"),
    path("agents/<uuid:pk>/health/", views.HealthCheckCardView.as_view(), name="card_health"),
    path("agents/<uuid:pk>/approval/", views.CardApprovalView.as_view(), name="card_approval"),
    path("agents/<uuid:card_pk>/contracts/new/", manage.ContractFormView.as_view(), name="contract_create"),
    path("contracts/", views.ContractListView.as_view(), name="contracts"),
    path("contracts/<uuid:pk>/edit/", manage.ContractFormView.as_view(), name="contract_edit"),
    path("contracts/<uuid:pk>/revoke/", manage.ContractRevokeView.as_view(), name="contract_revoke"),
    path("skills/", views.SkillListView.as_view(), name="skills"),
    path("skills/download/", views.SkillDownloadView.as_view(), name="skills_download"),
    path("skills/<uuid:pk>/", views.SkillDetailView.as_view(), name="skill_detail"),
    path("skills/<uuid:pk>/review/", views.SkillReviewView.as_view(), name="skill_review"),
    path("skills/<uuid:pk>/download/", views.SkillDownloadView.as_view(), name="skill_download"),
    path("tasks/", views.TaskListView.as_view(), name="tasks"),
    path("tasks/<uuid:pk>/", views.TaskDetailView.as_view(), name="task_detail"),
]
