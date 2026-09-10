from django.urls import path

from apps.approvals import views

app_name = "approvals"

urlpatterns = [
    path("", views.ApprovalCentreView.as_view(), name="centre"),
    path("<uuid:pk>/", views.ApprovalDetailView.as_view(), name="detail"),
    path("<uuid:pk>/<str:decision>/", views.ApprovalDecisionView.as_view(), name="decide"),
]
