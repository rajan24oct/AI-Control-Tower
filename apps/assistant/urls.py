from django.urls import path

from apps.assistant import views

app_name = "assistant"

urlpatterns = [
    path("ask/", views.AskView.as_view(), name="ask"),
    path("docs/<slug:section_id>/", views.SectionView.as_view(), name="section"),
]
