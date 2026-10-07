"""Discovery and review: the registry half of the A2A Gateway.

What an agent offers is learned from its card. An external agent's card is
read from where it publishes it; a mailbox agent's is written by the platform
from the registry and the skills its owner declares. Either way, what other
agents may ask of it is decided here -- by a person, recorded in the audit
trail -- and the gateway serves only what was decided. Discovery can add skills
to the registry, but it can never make one callable on its own.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from apps.a2a.models import DEFAULT_MODES, A2AAgentCard, A2ASkill, SkillStatus
from apps.a2a.services import setting
from apps.audit.models import AuditAction
from apps.audit.services import AuditService, snapshot
from apps.core.constants import HealthStatus
from integrations.base.a2a import PROTOCOL_VERSION, A2ACardInfo, A2ASkillDefinition
from integrations.base.types import HealthCheckResult

logger = logging.getLogger(__name__)

#: The Control Tower's credentials, as the published cards describe them.
SECURITY_SCHEMES = {
    "controlTowerKey": {
        "type": "apiKey", "in": "header", "name": "X-API-Key",
        "description": "A Control Tower service-account key (act_...). The key's role needs a2a.invoke.",
    },
    "controlTowerBearer": {
        "type": "http", "scheme": "bearer",
        "description": "The same service-account key, as a bearer token.",
    },
}


@dataclass
class SyncResult:
    card: A2AAgentCard
    ok: bool = True
    error: str = ""
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    unchanged: int = 0

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.changed or self.removed or self.restored)

    @property
    def summary(self) -> str:
        if not self.ok:
            return f"Could not read {self.card.name}'s card: {self.error}"
        parts = [
            f"{len(names)} {label}"
            for names, label in (
                (self.added, "new"),
                (self.changed, "changed"),
                (self.restored, "returned"),
                (self.removed, "withdrawn"),
            )
            if names
        ]
        if not parts:
            return f"{self.card.name}: {self.unchanged} skill(s), nothing changed."
        return f"{self.card.name}: {', '.join(parts)}, {self.unchanged} unchanged."

    def as_metadata(self) -> dict:
        return {
            "added": self.added, "changed": self.changed, "restored": self.restored,
            "removed": self.removed, "unchanged": self.unchanged,
        }


def declared_definitions(card: A2AAgentCard) -> list[A2ASkillDefinition]:
    """The skills a mailbox agent's owner declares, as definitions."""
    return [
        A2ASkillDefinition(
            id=str(item["id"]), name=str(item.get("name") or ""), description=str(item.get("description") or ""),
            tags=[str(tag) for tag in item.get("tags") or []],
            examples=[str(example) for example in item.get("examples") or []],
        )
        for item in card.declared_skills or []
        if isinstance(item, dict) and item.get("id")
    ]


def internal_card(card: A2AAgentCard) -> dict:
    """The card the platform writes for an agent that has no endpoint of its own."""
    agent = card.agent
    return {
        "name": card.name,
        "description": card.description or (agent.business_purpose if agent else ""),
        "version": "1.0.0",
        "protocolVersion": PROTOCOL_VERSION,
        "defaultInputModes": DEFAULT_MODES,
        "defaultOutputModes": DEFAULT_MODES,
        "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": True},
        "skills": [definition.to_wire() for definition in declared_definitions(card)],
    }


def published_card(card: A2AAgentCard, *, url: str) -> dict:
    """The Agent Card other agents are shown: the reviewed skills, at the gateway's address.

    Never the card as the agent last described itself. A skill that changed
    since review, or was never reviewed, is not on it; and the URL is the
    gateway's, so a client that follows the card comes back through governance.
    """
    skills = card.skills.filter(status=SkillStatus.ENABLED).order_by("skill_id")
    source = card.card or {}
    modes_in = source.get("defaultInputModes") or DEFAULT_MODES
    modes_out = source.get("defaultOutputModes") or DEFAULT_MODES
    wire = {
        "protocolVersion": PROTOCOL_VERSION,
        "name": card.name,
        "description": card.description or source.get("description") or (card.agent.business_purpose if card.agent_id else ""),
        "url": url,
        "preferredTransport": "JSONRPC",
        "version": card.agent_version or source.get("version") or "1.0.0",
        "provider": {
            "organization": card.publisher or card.organization.name,
            "url": card.documentation_url or url,
        },
        "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": True},
        "defaultInputModes": modes_in,
        "defaultOutputModes": modes_out,
        "skills": [skill.to_wire() for skill in skills],
        "securitySchemes": SECURITY_SCHEMES,
        "security": [{"controlTowerKey": []}, {"controlTowerBearer": []}],
    }
    if card.documentation_url:
        wire["documentationUrl"] = card.documentation_url
    return wire


class A2ARegistryService:
    @classmethod
    def sync(cls, card: A2AAgentCard, *, actor=None) -> SyncResult:
        """Read the agent's card and reconcile the registry with it.

        Never raises for an agent that cannot be reached. The failure is recorded
        on the card and returned: "we could not reach it" is a result an
        operator needs to see, not an exception for them to decode.
        """
        if not card.is_gateway_reachable:
            return SyncResult(
                card, ok=False, error=f"{card.get_transport_display()} agents cannot be reached by the gateway."
            )

        started = time.monotonic()
        try:
            info = cls._read(card)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:500]
            logger.warning("A2A card sync failed for %s: %s", card.name, error)
            card.last_sync_error = error
            card.health_status = HealthStatus.UNHEALTHY
            card.last_health_check = timezone.now()
            card.last_health_message = error
            card.save(update_fields=[
                "last_sync_error", "health_status", "last_health_check", "last_health_message", "updated_at",
            ])
            return SyncResult(card, ok=False, error=error)

        latency = int((time.monotonic() - started) * 1000)
        now = timezone.now()
        with transaction.atomic():
            result = cls._reconcile(card, info.skills, now)
            card.card = info.raw
            card.card_hash = info.card_hash()
            card.protocol_version = info.protocol_version[:20]
            card.agent_version = info.version[:50]
            card.capabilities = info.capabilities
            card.last_synced_at = now
            card.last_sync_error = ""
            if card.uses_mailbox:
                cls._mailbox_health(card, now)
            else:
                card.health_status = HealthStatus.HEALTHY
                card.last_health_check = now
                card.last_health_latency_ms = latency
                card.last_health_message = ""
            card.save(update_fields=[
                "card", "card_hash", "protocol_version", "agent_version", "capabilities", "last_synced_at",
                "last_sync_error", "health_status", "last_health_check", "last_health_latency_ms",
                "last_health_message", "updated_at",
            ])

        if result.has_changes:
            # A sync that changed nothing is not worth a trail entry; one that
            # changed what agents could be asked for is.
            AuditService.log(
                action=AuditAction.CONFIG_CHANGE,
                resource_type=card._meta.label,
                resource_id=card.pk,
                resource_label=f"{card.name} skills",
                organization=card.organization,
                actor=actor,
                new_value=result.as_metadata(),
                metadata={"operation": "a2a_discovery"},
            )
        return result

    @staticmethod
    def _read(card: A2AAgentCard) -> A2ACardInfo:
        if card.uses_mailbox:
            return A2ACardInfo.from_wire(internal_card(card))
        return card.get_adapter(timeout=float(setting("CALL_TIMEOUT_SECONDS", 30))).get_card()

    @staticmethod
    def _mailbox_health(card: A2AAgentCard, now) -> None:
        """A mailbox agent is as healthy as the agent collecting from it is free to."""
        agent = card.agent
        card.last_health_check = now
        card.last_health_latency_ms = None
        if agent is not None and agent.is_suspended:
            card.health_status, card.last_health_message = HealthStatus.UNHEALTHY, "Suspended by its kill-switch."
        elif agent is not None and not agent.is_active:
            card.health_status, card.last_health_message = (
                HealthStatus.UNHEALTHY, f"The agent is {agent.get_status_display().lower()}."
            )
        else:
            card.health_status, card.last_health_message = HealthStatus.HEALTHY, "Mailbox held by the gateway."

    @classmethod
    def _reconcile(cls, card: A2AAgentCard, definitions, now) -> SyncResult:
        review = setting("REVIEW_NEW_SKILLS", True)
        first_status = SkillStatus.PENDING_REVIEW if review else SkillStatus.ENABLED
        result = SyncResult(card)
        existing = {skill.skill_id: skill for skill in card.skills.all()}
        seen: set[str] = set()

        for definition in definitions:
            skill_id = definition.id[:128]
            if skill_id in seen:
                continue  # a card listing one id twice: the first wins
            seen.add(skill_id)
            digest = definition.definition_hash()
            skill = existing.get(skill_id)

            if skill is None:
                A2ASkill.objects.create(
                    organization_id=card.organization_id,
                    card=card,
                    skill_id=skill_id,
                    definition_hash=digest,
                    status=first_status,
                    # A skill says nothing about what it does to the world, so a
                    # reviewer starts from the agent's own risk.
                    risk_level=card.risk_level,
                    first_seen_at=now,
                    last_seen_at=now,
                    **cls._fields(definition),
                )
                result.added.append(skill_id)
                continue

            skill.last_seen_at = now
            if skill.status == SkillStatus.REMOVED:
                # Gone and back again: it is looked at afresh.
                skill.status = first_status
                result.restored.append(skill_id)

            if skill.definition_hash != digest:
                for attribute, value in cls._fields(definition).items():
                    setattr(skill, attribute, value)
                skill.definition_hash = digest
                if review and skill.status in {SkillStatus.ENABLED, SkillStatus.DISABLED}:
                    # The rug-pull case: a skill's description changing underneath
                    # the decision made about it -- to ask for more than was agreed.
                    # It stops being reachable until someone has looked again.
                    skill.changed_since_review = True
                    if skill.status == SkillStatus.ENABLED:
                        skill.status = SkillStatus.PENDING_REVIEW
                result.changed.append(skill_id)
            elif skill_id not in result.restored:
                result.unchanged += 1
            skill.save()

        for skill_id, skill in existing.items():
            if skill_id not in seen and skill.status != SkillStatus.REMOVED:
                # Kept, not deleted: the log still points at it.
                skill.status = SkillStatus.REMOVED
                skill.save(update_fields=["status", "updated_at"])
                result.removed.append(skill_id)
        return result

    @staticmethod
    def _fields(definition: A2ASkillDefinition) -> dict:
        return {
            "name": definition.name[:200],
            "description": definition.description,
            "tags": definition.tags,
            "examples": definition.examples,
            "input_modes": definition.input_modes,
            "output_modes": definition.output_modes,
        }

    # -- Governance ---------------------------------------------------------

    @staticmethod
    def review(skill: A2ASkill, *, enable: bool, actor, note: str = "") -> None:
        """Enable or disable a skill. The decision, not discovery, is what serves it."""
        if skill.status == SkillStatus.REMOVED:
            raise ValueError(f"'{skill.skill_id}' is no longer offered by {skill.card.name}.")

        fields = ["status", "changed_since_review", "review_note"]
        before = snapshot(skill, fields)
        skill.status = SkillStatus.ENABLED if enable else SkillStatus.DISABLED
        skill.changed_since_review = False
        skill.reviewed_at = timezone.now()
        skill.reviewed_by = actor if getattr(actor, "pk", None) else None
        skill.review_note = note[:500]
        skill.save(update_fields=[*fields, "reviewed_at", "reviewed_by", "updated_at"])
        AuditService.log(
            action=AuditAction.APPROVE if enable else AuditAction.REJECT,
            resource_type=skill._meta.label,
            resource_id=skill.pk,
            resource_label=skill.qualified_name,
            organization=skill.organization,
            actor=actor,
            old_value=before,
            new_value=snapshot(skill, fields),
            # The hash pins *which* definition was approved, so a later "this
            # changed after approval" can be checked against the trail.
            metadata={"agent": skill.card.name, "definition_hash": skill.definition_hash},
        )

    @staticmethod
    def set_approval(card: A2AAgentCard, *, approved: bool, actor) -> None:
        fields = ["is_approved", "approved_at", "approved_by"]
        before = snapshot(card, fields)
        card.is_approved = approved
        card.approved_at = timezone.now() if approved else None
        card.approved_by = actor if approved and getattr(actor, "pk", None) else None
        card.save(update_fields=[*fields, "updated_at"])
        AuditService.log(
            action=AuditAction.APPROVE if approved else AuditAction.REJECT,
            resource_type=card._meta.label,
            resource_id=card.pk,
            resource_label=str(card),
            organization=card.organization,
            actor=actor,
            old_value=before,
            new_value=snapshot(card, fields),
        )

    @classmethod
    def health_check(cls, card: A2AAgentCard) -> HealthCheckResult | None:
        """Probe the agent and record the answer. None if the gateway cannot reach it at all."""
        if not card.is_gateway_reachable:
            return None
        now = timezone.now()
        if card.uses_mailbox:
            cls._mailbox_health(card, now)
            check = HealthCheckResult(
                healthy=card.health_status == HealthStatus.HEALTHY, status=card.health_status,
                message=card.last_health_message, checked_at=now,
            )
        else:
            try:
                check = card.get_adapter(timeout=float(setting("CALL_TIMEOUT_SECONDS", 30))).health_check()
            except Exception as exc:  # misconfiguration: no endpoint, no credential
                check = HealthCheckResult(
                    healthy=False, status="unhealthy", message=f"{type(exc).__name__}: {exc}"[:500], checked_at=now,
                )
            card.health_status = HealthStatus.HEALTHY if check.healthy else HealthStatus.UNHEALTHY
            card.last_health_check = now
            card.last_health_latency_ms = check.latency_ms
            card.last_health_message = (check.message or "")[:500]
        card.save(update_fields=[
            "health_status", "last_health_check", "last_health_latency_ms", "last_health_message", "updated_at",
        ])
        return check
