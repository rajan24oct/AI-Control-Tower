from django.urls import path

from apps.executions.consumers import ExecutionMonitorConsumer

websocket_urlpatterns = [
    path(
        "ws/executions/<uuid:organization_id>/",
        ExecutionMonitorConsumer.as_asgi(),
        name="execution_monitor",
    ),
]
