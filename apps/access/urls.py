from django.urls import path
from django.views.generic import RedirectView

from apps.access import site_admin, views

app_name = "access"

urlpatterns = [
    # Site administration: every organization, for superusers.
    path("organizations/", site_admin.OrganizationListView.as_view(), name="organizations"),
    path("organizations/new/", site_admin.OrganizationFormView.as_view(), name="organization_create"),
    path("organizations/<uuid:pk>/", site_admin.OrganizationDetailView.as_view(), name="organization_detail"),
    path("organizations/<uuid:pk>/edit/", site_admin.OrganizationFormView.as_view(), name="organization_edit"),
    path("organizations/<uuid:org>/members/new/", site_admin.SiteMemberCreateView.as_view(), name="organization_member_create"),
    path("organizations/<uuid:org>/members/<uuid:pk>/", site_admin.SiteMemberEditView.as_view(), name="organization_member_edit"),
    path("organizations/<uuid:org>/members/<uuid:pk>/remove/", site_admin.SiteMemberRemoveView.as_view(),
         name="organization_member_remove"),
    path("organizations/<uuid:org>/members/<uuid:pk>/password-link/", site_admin.SiteMemberPasswordLinkView.as_view(),
         name="organization_member_password_link"),
    # The active organization.
    path("", RedirectView.as_view(pattern_name="access:members"), name="index"),
    path("members/", views.MemberListView.as_view(), name="members"),
    path("members/new/", views.MemberCreateView.as_view(), name="member_create"),
    path("members/<uuid:pk>/", views.MemberEditView.as_view(), name="member_edit"),
    path("members/<uuid:pk>/remove/", views.MemberRemoveView.as_view(), name="member_remove"),
    path("members/<uuid:pk>/password-link/", views.MemberPasswordLinkView.as_view(), name="member_password_link"),
    path("teams/", views.TeamListView.as_view(), name="teams"),
    path("teams/new/", views.TeamFormView.as_view(), name="team_create"),
    path("teams/<uuid:pk>/", views.TeamFormView.as_view(), name="team_edit"),
    path("teams/<uuid:pk>/delete/", views.TeamDeleteView.as_view(), name="team_delete"),
    path("service-accounts/", views.ServiceAccountListView.as_view(), name="service_accounts"),
    path("service-accounts/new/", views.ServiceAccountFormView.as_view(), name="service_account_create"),
    path("service-accounts/<uuid:pk>/", views.ServiceAccountDetailView.as_view(), name="service_account_detail"),
    path("service-accounts/<uuid:pk>/edit/", views.ServiceAccountFormView.as_view(), name="service_account_edit"),
    path("service-accounts/<uuid:pk>/keys/new/", views.IssueKeyView.as_view(), name="key_issue"),
    path("keys/<uuid:pk>/revoke/", views.RevokeKeyView.as_view(), name="key_revoke"),
]
