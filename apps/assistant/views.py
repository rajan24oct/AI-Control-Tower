"""The assistant's endpoints: a question in, an answer out; and the source pages."""

from __future__ import annotations

import json
import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, JsonResponse
from django.views import View
from django.views.generic import TemplateView

from apps.assistant.answers import MAX_QUESTION, AssistantService
from apps.assistant.knowledge import knowledge, render

logger = logging.getLogger(__name__)


class AskView(LoginRequiredMixin, View):
    """POST {"question": "..."} and get {"html", "sources", "links", "suggestions", "mode"}."""

    raise_exception = True  # an expired session gets a 403 the widget can explain, not a login page

    def post(self, request, *args, **kwargs):
        try:
            payload = json.loads(request.body or b"{}")
        except ValueError:
            payload = request.POST
        question = str(payload.get("question", "")).strip()
        if len(question) > MAX_QUESTION:
            return JsonResponse({"error": f"Please keep questions under {MAX_QUESTION} characters."}, status=400)
        history = payload.get("history") if isinstance(payload, dict) else None
        answer = AssistantService.answer(
            question, organization=request.organization, user=request.user,
            history=history if isinstance(history, list) else None,
        )
        logger.info("Assistant answered (%s): %s", answer.mode, question[:120])
        return JsonResponse(answer.as_dict())


class SectionView(LoginRequiredMixin, TemplateView):
    """One section of the documentation, so every answer can show its source."""

    template_name = "assistant/section.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        base = knowledge()
        section = base.get(kwargs["section_id"])
        if section is None:
            raise Http404("No such section.")
        siblings = [s for s in base.sections if s.doc == section.doc]
        index = siblings.index(section)
        context.update({
            "section": section,
            "body_html": render(section.body),
            "previous": siblings[index - 1] if index else None,
            "next": siblings[index + 1] if index + 1 < len(siblings) else None,
            "page_title": section.title,
            "page_subtitle": f"{section.doc_title} · {section.breadcrumb}",
        })
        return context
