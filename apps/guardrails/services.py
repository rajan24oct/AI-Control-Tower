"""Applying guardrail policies to a piece of text.

`GuardrailService.check` runs every active policy that covers the traffic,
then settles on one verdict: the strictest action wins (block, then redact,
then warn). A redaction replaces exactly the spans the redacting policies
matched; warnings change nothing but are recorded. Each policy that hit writes
one event, so the record shows every control that fired, not only the one that
decided.

The check is synchronous and pattern-based, so it is cheap enough to sit on the
request path of both gateways.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from apps.guardrails import detectors
from apps.guardrails.models import (
    ACTION_RANK,
    Detector,
    GuardAction,
    GuardrailEvent,
    GuardrailPolicy,
    Source,
)
from apps.core.constants import LifecycleStatus


def mask(value: str) -> str:
    """Enough to recognise what matched, never enough to reuse it."""
    value = value.strip()
    if len(value) <= 6:
        return "•" * len(value)
    shown = 2 if len(value) < 16 else 4
    return f"{value[:shown]}{'•' * min(len(value) - shown, 12)} ({len(value)} chars)"


@dataclass
class PolicyHit:
    policy: GuardrailPolicy
    hits: list[detectors.Hit]

    @property
    def score(self) -> float | None:
        if self.policy.detector == Detector.PROMPT_INJECTION:
            return detectors.injection_score(self.hits)
        return None

    def findings(self, text: str) -> list[dict]:
        seen, rows = set(), []
        for hit in self.hits:
            snippet = text[hit.start:hit.end]
            key = (hit.kind, snippet)
            if key in seen:
                continue
            seen.add(key)
            # Injection phrases are not secret; showing them helps a reviewer.
            shown = snippet[:120] if hit.detector == Detector.PROMPT_INJECTION else mask(snippet)
            rows.append({"kind": hit.kind, "label": hit.label, "snippet": shown})
        return rows


@dataclass
class Verdict:
    text: str
    action: str = ""
    matches: list[PolicyHit] = field(default_factory=list)
    events: list[GuardrailEvent] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.action == GuardAction.BLOCK

    @property
    def redacted(self) -> bool:
        return self.action == GuardAction.REDACT

    @property
    def deciding(self) -> list[PolicyHit]:
        return [m for m in self.matches if m.policy.action == self.action]

    @property
    def reason(self) -> str:
        if not self.matches:
            return ""
        names = ", ".join(sorted({m.policy.name for m in self.deciding}))
        kinds = ", ".join(sorted({h.label for m in self.deciding for h in m.hits}))
        return f"Guardrail '{names}': {kinds}."

    def as_detail(self) -> dict:
        return {
            "action": self.action,
            "policies": sorted({m.policy.name for m in self.matches}),
            "detectors": sorted({m.policy.detector for m in self.matches}),
            "events": [str(e.pk) for e in self.events],
        }


class GuardrailService:
    @staticmethod
    def policies(organization):
        return GuardrailPolicy.objects.for_organization(organization).filter(status=LifecycleStatus.ACTIVE)

    @classmethod
    def check(cls, organization, text: str, *, stage: str, source: str, agent=None, environment: str = "",
              reference: str = "", record: bool = True, policies=None) -> Verdict:
        verdict = Verdict(text=text or "")
        if not text:
            return verdict
        tier = getattr(agent, "risk_tier", None)
        candidates = cls.policies(organization) if policies is None else policies
        for policy in candidates:
            if not policy.covers(stage=stage, source=source, tier=tier, environment=environment):
                continue
            hits = detectors.detect(policy.detector, text, policy.config)
            if hits:
                verdict.matches.append(PolicyHit(policy, hits))
        if not verdict.matches:
            return verdict

        verdict.action = max((m.policy.action for m in verdict.matches), key=lambda a: ACTION_RANK[a])
        if verdict.redacted:
            spans = [h for m in verdict.matches if m.policy.action == GuardAction.REDACT for h in m.hits]
            verdict.text = detectors.redact(text, spans)
        if record:
            verdict.events = [
                GuardrailEvent.objects.create(
                    organization=organization, policy=m.policy if m.policy.pk else None,
                    policy_name=m.policy.name, detector=m.policy.detector, stage=stage, action=m.policy.action,
                    source=source, agent=agent, environment=environment or "", findings=m.findings(text),
                    score=m.score, reference=(reference or "")[:200],
                )
                for m in verdict.matches
            ]
        return verdict

    @classmethod
    def check_messages(cls, organization, messages: list, **kwargs) -> tuple[list, Verdict | None]:
        """Check chat messages one by one, redacting in place; the strictest verdict is returned."""
        worst, out = None, []
        for message in messages:
            if not isinstance(message, dict) or not isinstance(message.get("content"), str):
                out.append(message)
                continue
            verdict = cls.check(organization, message["content"], **kwargs)
            if verdict.redacted:
                message = {**message, "content": verdict.text}
            out.append(message)
            if verdict.action and (worst is None or ACTION_RANK[verdict.action] > ACTION_RANK[worst.action]):
                worst = verdict
        return out, worst


def summary(organization, *, days: int = 30) -> dict:
    from datetime import timedelta

    from django.db.models import Count
    from django.utils import timezone

    since = timezone.now() - timedelta(days=days)
    events = GuardrailEvent.objects.for_organization(organization).filter(created_at__gte=since)
    by_action = dict(events.values_list("action").annotate(n=Count("id")))
    labels = dict(Detector.choices)
    by_detector = [
        {"key": key, "label": labels.get(key, key), "count": n}
        for key, n in events.values_list("detector").annotate(n=Count("id")).order_by("-n")
    ]
    by_agent = list(
        events.exclude(agent=None).values("agent__id", "agent__name").annotate(n=Count("id")).order_by("-n")[:6]
    )
    by_source = dict(events.values_list("source").annotate(n=Count("id")))
    return {
        "days": days,
        "total": sum(by_action.values()),
        "blocked": by_action.get(GuardAction.BLOCK, 0),
        "redacted": by_action.get(GuardAction.REDACT, 0),
        "warned": by_action.get(GuardAction.WARN, 0),
        "by_detector": by_detector,
        "by_agent": by_agent,
        "by_source": [{"label": dict(Source.choices).get(k, k), "count": v} for k, v in by_source.items()],
    }
