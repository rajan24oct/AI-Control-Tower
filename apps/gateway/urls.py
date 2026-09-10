from django.urls import path

from apps.gateway import views

app_name = "gateway"

urlpatterns = [
    path("", views.GatewayOverviewView.as_view(), name="overview"),
    path("messages/", views.GatewayMessageListView.as_view(), name="messages"),
]
