"""ChatGPT for the IRGF Agent: OpenAI's Chat Completions API, with the agent's
skills as instructions and their tools as functions.

The model answers by looking things up: each turn it may call the skills'
tools (search the framework reference or the platform docs, define a code,
read decision rights or the organization's status), and the loop runs those
calls and hands back the results until the model writes its answer. Sources
come from what the tools actually returned, not from what the model claims.

Standard library only, like the platform's other HTTP adapters: no SDK to
install. The API key is read from the environment variable `ASSISTANT_OPENAI_KEY_ENV`
names (`OPENAI_API_KEY` by default), never stored. `ASSISTANT_OPENAI_BASE_URL`
points it at any OpenAI-compatible endpoint.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from django.conf import settings

from apps.assistant.skills import Skill, ToolContext, load_skills, run_tool, tool_specs

logger = logging.getLogger(__name__)

#: A question that needs more look-ups than this is going in circles.
MAX_TOOL_ROUNDS = 5
#: Earlier turns sent with each question, so a follow-up has its context.
HISTORY_TURNS = 8
HISTORY_CHARS = 1500


class ChatGPTUnavailable(Exception):
    """ChatGPT could not answer: not configured, refused, or unreachable."""


@dataclass
class ChatGPTReply:
    text: str
    sources: list[dict] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


def api_key() -> str:
    return os.environ.get(getattr(settings, "ASSISTANT_OPENAI_KEY_ENV", "OPENAI_API_KEY"), "").strip()


def configured() -> bool:
    return getattr(settings, "ASSISTANT_PROVIDER", "openai") == "openai" and bool(api_key())


def system_prompt(skills: list[Skill], agent_name: str, organization) -> str:
    sections = "\n\n".join(f"<skill name=\"{s.name}\">\n{s.instructions}\n</skill>" for s in skills)
    org = f"The user is working in the organization \"{organization.name}\"." if organization else ""
    return (
        f"You are the {agent_name}, the assistant inside the AI Control Tower. You help people understand the "
        "Intent-to-Runtime Governance Framework (IRGF, https://irgframework.org/) and how the AI Control Tower "
        f"applies it. {org}\n\n"
        "Use the skills below. Ground every answer in what your tools return; if they do not cover the question, "
        "say so plainly. Keep answers short and practical: a direct answer first, then the detail that matters. "
        "Use Markdown: short paragraphs, bullet lists, and a table when it helps. Do not paste URLs for sources; "
        "the interface lists the sections you looked up. Politely decline questions unrelated to IRGF, AI "
        "governance or the AI Control Tower.\n\n" + sections
    )


def history_messages(history) -> list[dict]:
    """Earlier turns from the browser, checked: only roles and text, length-limited."""
    messages = []
    for turn in (history or [])[-HISTORY_TURNS:]:
        if not isinstance(turn, dict):
            continue
        role = {"user": "user", "bot": "assistant", "assistant": "assistant"}.get(turn.get("role"))
        text = str(turn.get("text") or "").strip()[:HISTORY_CHARS]
        if role and text:
            messages.append({"role": role, "content": text})
    return messages


def _post(payload: dict) -> dict:
    base = getattr(settings, "ASSISTANT_OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    request = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key()}"},
        method="POST",
    )
    timeout = float(getattr(settings, "ASSISTANT_OPENAI_TIMEOUT", 45))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", {}).get("message", "")
        except Exception:
            pass
        reason = {401: "the API key was refused", 403: "the API key may not use this model",
                  404: "the model was not found", 429: "the rate limit or quota was reached"}.get(exc.code, f"HTTP {exc.code}")
        raise ChatGPTUnavailable(f"ChatGPT is unavailable: {reason}. {detail}".strip()) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ChatGPTUnavailable(f"ChatGPT could not be reached: {exc}") from exc


def ask(question: str, *, history=None, organization=None, agent_name: str = "IRGF Agent") -> ChatGPTReply:
    if not configured():
        raise ChatGPTUnavailable("ChatGPT is not configured.")
    skills = load_skills()
    context = ToolContext(organization=organization)
    model = getattr(settings, "ASSISTANT_OPENAI_MODEL", "gpt-4o-mini")
    messages = [{"role": "system", "content": system_prompt(skills, agent_name, organization)},
                *history_messages(history),
                {"role": "user", "content": question}]
    tools = tool_specs(skills)
    reply = ChatGPTReply(text="", model=model)

    for round_ in range(MAX_TOOL_ROUNDS + 1):
        payload = {"model": model, "messages": messages, "temperature": 0.2, "max_tokens": 900}
        # On the last round the model must answer with what it has.
        if tools and round_ < MAX_TOOL_ROUNDS:
            payload["tools"] = tools
        body = _post(payload)
        usage = body.get("usage") or {}
        reply.input_tokens += int(usage.get("prompt_tokens") or 0)
        reply.output_tokens += int(usage.get("completion_tokens") or 0)
        choice = (body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        calls = message.get("tool_calls") or []
        if not calls:
            reply.text = (message.get("content") or "").strip()
            break
        messages.append({"role": "assistant", "content": message.get("content"), "tool_calls": calls})
        for call in calls:
            function = call.get("function") or {}
            name = function.get("name", "")
            reply.tools_used.append(name)
            messages.append({
                "role": "tool", "tool_call_id": call.get("id", ""),
                "content": run_tool(name, function.get("arguments", "{}"), context),
            })

    if not reply.text:
        raise ChatGPTUnavailable("ChatGPT returned no answer.")
    reply.sources, reply.links = context.sources[:4], context.links[:3]
    logger.info("IRGF Agent (ChatGPT %s) used %s; %s+%s tokens", model, reply.tools_used or "no tools",
                reply.input_tokens, reply.output_tokens)
    return reply
