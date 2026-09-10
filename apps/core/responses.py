"""Helpers for views that serve both AJAX and ordinary form posts.

Every action in the UI is a real form underneath. When jQuery intercepts it we
answer with JSON; when it does not, the same view redirects and flashes a
message exactly as before. That keeps the interface working without scripting
rather than merely degrading politely.
"""

from __future__ import annotations

from django.contrib import messages
from django.http import HttpResponseRedirect, JsonResponse


def is_ajax(request) -> bool:
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def action_response(
    request,
    *,
    message: str,
    redirect_to: str,
    tone: str = "success",
    payload: dict | None = None,
    status: int = 200,
):
    """Answer an action either as JSON or as a redirect with a flash message."""
    if is_ajax(request):
        body = {"ok": status < 400, "message": message, "tone": tone}
        body.update(payload or {})
        return JsonResponse(body, status=status)

    level = {
        "success": messages.SUCCESS,
        "warning": messages.WARNING,
        "danger": messages.ERROR,
    }.get(tone, messages.INFO)
    messages.add_message(request, level, message)
    return HttpResponseRedirect(redirect_to)
