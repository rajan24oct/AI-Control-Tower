from django.urls import path

from apps.notifications import views

app_name = "notifications"

urlpatterns = [
    path("", views.NotificationListView.as_view(), name="list"),
    path("bell/", views.NotificationBadgeView.as_view(), name="bell"),
    path("preferences/", views.NotificationPreferencesView.as_view(), name="preferences"),
    path("read/", views.MarkReadView.as_view(), name="mark_all_read"),
    path("<uuid:pk>/read/", views.MarkReadView.as_view(), name="mark_read"),
]
