from django.urls import path

from apps.lifecycle import views

app_name = "lifecycle"

urlpatterns = [
    # S7 · Gate G3
    path("authorizations/", views.AuthorizationListView.as_view(), name="authorizations"),
    path("authorizations/<uuid:pk>/", views.AuthorizationDetailView.as_view(), name="authorization_detail"),
    path("authorizations/<uuid:pk>/decide/", views.AuthorizationDecisionView.as_view(), name="authorization_decide"),
    path("authorizations/<uuid:pk>/revoke/", views.AuthorizationRevokeView.as_view(), name="authorization_revoke"),
    path("conditions/<uuid:pk>/met/", views.ConditionMetView.as_view(), name="condition_met"),
    path("agents/<uuid:pk>/authorize/", views.AuthorizationRequestView.as_view(), name="authorization_request"),
    path(
        "applications/<uuid:pk>/authorize/", views.ApplicationAuthorizationRequestView.as_view(),
        name="application_authorization_request",
    ),
    # S9 · Gate G4
    path("changes/", views.ChangeListView.as_view(), name="changes"),
    path("changes/<uuid:pk>/", views.ChangeDetailView.as_view(), name="change_detail"),
    path("changes/<uuid:pk>/decide/", views.ChangeDecisionView.as_view(), name="change_decide"),
    path("changes/<uuid:pk>/reclassify/", views.ChangeReclassifyView.as_view(), name="change_reclassify"),
    path("changes/<uuid:pk>/confirm/", views.ChangeConfirmView.as_view(), name="change_confirm"),
    # M17
    path("exceptions/", views.ExceptionListView.as_view(), name="exceptions"),
    path("exceptions/new/", views.ExceptionRequestView.as_view(), name="exception_request"),
    path("exceptions/<uuid:pk>/", views.ExceptionDetailView.as_view(), name="exception_detail"),
    path("exceptions/<uuid:pk>/decide/", views.ExceptionDecisionView.as_view(), name="exception_decide"),
    path("exceptions/<uuid:pk>/revoke/", views.ExceptionRevokeView.as_view(), name="exception_revoke"),
]
