"""Prompt and completion text on the observability surface.

The Control Tower captures no prompt text implicitly. Engines report it in the
metadata of the events that already carry the interaction -- PROMPT_CREATED for
what went in, MODEL_REQUEST_COMPLETED for what came back -- and this module is
the only place that reads it.

Two controls sit between stored text and a screen, because this text is the one
place customer data reliably shows up:

  * `prompt.view`, held by every role except Viewer, so an organization can put
    a broad audience on the monitor without handing them content.
  * Redaction by the agent's own data classification, which applies even to
    holders of the permission -- a shared operations screen must not become an
    accidental data export just because the person watching is senior.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

from apps.accounts.roles import Perm
from apps.executions.pii import highlight, scan, summarise

#: Metadata keys carrying the text, in preference order. Aliases are accepted
#: for the same reason the webhook adapter maps status vocabularies: engines
#: arrive with the names they already use, and rejecting them buys nothing.
PROMPT_KEYS = ("prompt", "input", "messages")
COMPLETION_KEYS = ("completion", "output", "response")

#: Which event carries which half of the exchange.
PROMPT_EVENTS = ("PROMPT_CREATED",)
COMPLETION_EVENTS = ("MODEL_REQUEST_COMPLETED",)

REDACTION_NOTICE = "Redacted — this agent handles {classification} data."
UNAVAILABLE_NOTICE = "The engine did not report this."


def _config() -> dict:
    return settings.CONTROL_TOWER.get("PROMPT_DISPLAY", {})


def max_chars() -> int:
    return int(_config().get("MAX_CHARS", 1200))


def redacted_classifications() -> frozenset[str]:
    return frozenset(_config().get("REDACT_CLASSIFICATIONS", ()))


@dataclass(frozen=True)
class Exchange:
    """What one execution said and heard, as far as it may be shown."""

    prompt: str = ""
    completion: str = ""
    redacted: bool = False
    classification: str = ""
    truncated: bool = False
    #: `[{key, label, count}]` for identifiers matched in the text. Populated
    #: even when the text itself is withheld: "this classified run carried two
    #: card numbers" is exactly the signal a governance review needs, and it
    #: reveals nothing the redaction was protecting.
    pii: tuple = ()

    @property
    def has_content(self) -> bool:
        return bool(self.prompt or self.completion)

    @property
    def has_pii(self) -> bool:
        return bool(self.pii)

    @property
    def pii_count(self) -> int:
        return sum(entry["count"] for entry in self.pii)

    @property
    def pii_summary(self) -> str:
        """For a tooltip: "2 payment card numbers, 1 email address"."""
        return ", ".join(
            f"{entry['count']} {entry['label'].lower()}{'s' if entry['count'] > 1 else ''}"
            for entry in self.pii
        )

    @property
    def prompt_html(self):
        return highlight(self.prompt)

    @property
    def completion_html(self):
        return highlight(self.completion)

    @property
    def notice(self) -> str:
        if self.redacted:
            return REDACTION_NOTICE.format(classification=self.classification or "classified")
        return ""


#: A caller without the permission gets this rather than a partial object, so
#: templates never have to reason about why a field is empty.
FORBIDDEN = Exchange()


def _first_value(metadata: dict, keys: tuple[str, ...]) -> str:
    if not isinstance(metadata, dict):
        return ""
    for key in keys:
        value = metadata.get(key)
        if value in (None, "", [], {}):
            continue
        # A messages array is the common shape; render it as a transcript
        # rather than as JSON, which is unreadable at a glance.
        if isinstance(value, list):
            return _flatten_messages(value)
        return str(value)
    return ""


def _flatten_messages(messages: list) -> str:
    lines = []
    for message in messages:
        if isinstance(message, dict):
            role = str(message.get("role", "")).strip()
            content = message.get("content", "")
            if isinstance(content, list):
                # Multi-part content: keep the text parts, name the rest.
                parts = [
                    str(part.get("text", f"[{part.get('type', 'part')}]"))
                    if isinstance(part, dict)
                    else str(part)
                    for part in content
                ]
                content = "\n".join(parts)
            lines.append(f"{role}: {content}".strip(": ").strip() if role else str(content))
        else:
            lines.append(str(message))
    return "\n\n".join(line for line in lines if line)


def _truncate(text: str) -> tuple[str, bool]:
    limit = max_chars()
    if len(text) <= limit:
        return text, False
    # Cut on a word boundary when one is close, so the tail is not a half word.
    cut = text.rfind(" ", limit - 80, limit)
    return text[: cut if cut > 0 else limit].rstrip(), True


class PromptService:
    """Reads the reported exchange for executions, subject to the controls."""

    @staticmethod
    def may_view(viewer, organization) -> bool:
        if viewer is None or not getattr(viewer, "is_authenticated", False):
            return False
        if getattr(viewer, "is_superuser", False):
            return True
        return viewer.has_org_permission(organization, Perm.PROMPT_VIEW)

    @classmethod
    def attach(cls, executions, *, viewer, organization) -> list:
        """Set `.exchange` on each execution, in one extra query for the batch.

        Returns the same list, so it can be dropped into a view or a consumer
        without changing how the queryset is built.
        """
        executions = list(executions)
        if not executions:
            return executions

        if not cls.may_view(viewer, organization):
            for execution in executions:
                execution.exchange = FORBIDDEN
            return executions

        for execution in executions:
            execution.exchange = None

        by_id = {execution.pk: execution for execution in executions}
        for execution_id, prompt, completion in cls._latest_text(by_id.keys()):
            execution = by_id.get(execution_id)
            if execution is not None:
                execution.exchange = cls._build(execution, prompt, completion)

        for execution in executions:
            if execution.exchange is None:
                execution.exchange = cls._build(execution, "", "")
        return executions

    @classmethod
    def for_execution(cls, execution, *, viewer, organization) -> Exchange:
        return cls.attach([execution], viewer=viewer, organization=organization)[0].exchange

    # -- Internals ---------------------------------------------------------

    @staticmethod
    def _latest_text(execution_ids):
        """Yield `(execution_id, prompt, completion)` for the given runs.

        One query over the relevant events rather than one per execution: the
        monitor renders fifty rows at a time and re-renders them on every push.
        """
        from apps.executions.models import ExecutionEvent

        events = (
            ExecutionEvent.objects.filter(
                execution_id__in=list(execution_ids),
                event_type__in=list(PROMPT_EVENTS) + list(COMPLETION_EVENTS),
            )
            .order_by("execution_id", "sequence", "timestamp")
            .values_list("execution_id", "event_type", "metadata")
        )

        collected: dict = {}
        for execution_id, event_type, metadata in events:
            prompt, completion = collected.get(execution_id, ("", ""))
            if event_type in PROMPT_EVENTS:
                # First prompt wins: it is the one that started the run.
                prompt = prompt or _first_value(metadata, PROMPT_KEYS)
            else:
                # Last completion wins: it is the most recent thing said.
                completion = _first_value(metadata, COMPLETION_KEYS) or completion
            collected[execution_id] = (prompt, completion)

        for execution_id, (prompt, completion) in collected.items():
            yield execution_id, prompt, completion

    @staticmethod
    def _build(execution, prompt: str, completion: str) -> Exchange:
        classification = getattr(execution.agent, "data_classification", "") or ""

        # Scan the full text, before truncation: an identifier past the display
        # limit is still an identifier that went through the agent.
        pii = tuple(summarise(scan(prompt) + scan(completion)))

        if classification in redacted_classifications() and (prompt or completion):
            return Exchange(
                redacted=True,
                classification=execution.agent.get_data_classification_display().lower(),
                pii=pii,
            )

        prompt, prompt_cut = _truncate(prompt)
        completion, completion_cut = _truncate(completion)
        return Exchange(
            prompt=prompt,
            completion=completion,
            classification=classification,
            truncated=prompt_cut or completion_cut,
            pii=pii,
        )
