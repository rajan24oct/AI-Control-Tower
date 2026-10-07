"""A2A agent cards and contracts, managed in the web UI."""

from __future__ import annotations

import re

from django import forms
from django.core.exceptions import ValidationError

from apps.a2a.models import A2AAgentCard, A2AContract, CardOrigin, SkillStatus, sensitivity
from apps.core.constants import DataClassification
from apps.core.forms import ChoiceListField, JSONObjectField, OrganizationModelForm
from apps.inventory.forms import clean_credential_ref

SKILL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class SkillListField(forms.CharField):
    """The skills an owner declares, one per line: `id: what it does`.

    Nobody should have to type JSON to say what their agent can be asked for.
    """

    widget = forms.Textarea(attrs={"rows": 4, "class": "font-monospace"})

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("required", False)
        super().__init__(*args, **kwargs)

    def prepare_value(self, value):
        if isinstance(value, list):
            return "\n".join(
                f"{item['id']}: {item['description']}" if item.get("description") else str(item["id"])
                for item in value
                if isinstance(item, dict) and item.get("id")
            )
        return value

    def to_python(self, value):
        if isinstance(value, list):
            return value
        skills, seen = [], set()
        for number, line in enumerate((super().to_python(value) or "").splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            skill_id, _, description = line.partition(":")
            skill_id = skill_id.strip()
            if not SKILL_ID.match(skill_id):
                raise ValidationError(
                    f"Line {number}: “{skill_id}” is not a skill id. Use letters, digits, dots, dashes or underscores."
                )
            if skill_id in seen:
                raise ValidationError(f"Line {number}: “{skill_id}” is declared twice.")
            seen.add(skill_id)
            skills.append({
                "id": skill_id,
                "name": skill_id.replace("-", " ").replace("_", " ").capitalize(),
                "description": description.strip(),
            })
        return skills


class A2AAgentCardForm(OrganizationModelForm):
    declared_skills = SkillListField(
        label="Skills",
        help_text="One per line, as “id: what it does”. Other agents are shown each one only after governance reviews it.",
    )
    options = JSONObjectField(
        help_text='Static, non-secret headers or a timeout, e.g. {"headers": {"X-Tenant": "acme"}, "timeout": 20}.',
    )

    class Meta:
        model = A2AAgentCard
        fields = [
            "origin", "agent", "name", "slug", "description", "publisher", "documentation_url",
            "transport", "endpoint", "card_url", "auth_method", "credential_ref", "auth_header", "options",
            "declared_skills",
            "owner", "team", "data_classification", "risk_level", "access_mode", "status",
        ]
        labels = {
            "origin": "Agent", "agent": "Registry agent", "slug": "Address",
            "documentation_url": "Documentation URL", "card_url": "Agent Card URL",
            "auth_method": "Authentication", "credential_ref": "Credential reference", "auth_header": "Header name",
            "access_mode": "Who may send it work", "data_classification": "Clearance", "risk_level": "Risk",
        }
        help_texts = {
            "agent": "The agent this card publishes. Its agent_id becomes its address at the gateway.",
            "slug": "Where other agents reach it: /api/v1/a2a/<address>/. Blank derives it from the name; it cannot change later.",
            "transport": "An agent without an A2A endpoint of its own collects its work from the gateway's mailbox.",
            "endpoint": "Where the gateway sends A2A requests. The URL on the agent's own card is never followed.",
            "card_url": "Blank reads the card from the endpoint's /.well-known/agent-card.json.",
            "credential_ref": "The environment variable holding the token, e.g. PARTNER_A2A_TOKEN.",
            "access_mode": "Restricted by default: only agents holding a contract can reach it.",
            "data_classification": "The most sensitive data it may be sent. A contract can lower the ceiling, never raise it.",
        }

    unique_in_organization = ("name",)
    sections = (
        ("Agent", "An agent from this registry, or one another organization runs.",
         ["origin", "agent", "name", "slug", "description", "publisher", "documentation_url"]),
        ("Delivery", "How the gateway hands it work. The platform holds any credential; agents never hold each other's.",
         ["transport", "endpoint", "card_url", "auth_method", "credential_ref", "auth_header", "options"]),
        ("Skills", "What other agents may ask it for. An agent reached over HTTP publishes its own: sync its card once registered.",
         ["declared_skills"]),
        ("Governance", "Approval, skill review and contracts happen on its page once it is registered.",
         ["owner", "team", "data_classification", "risk_level", "access_mode", "status"]),
    )
    compact_fields = (
        "origin", "agent", "slug", "publisher", "documentation_url", "transport", "auth_method", "credential_ref",
        "auth_header", "owner", "team", "data_classification", "risk_level", "access_mode", "status",
    )
    show_when = {
        "agent": "origin:internal",
        "slug": "origin:external",
        "publisher": "origin:external",
        "endpoint": "transport:jsonrpc,mock",
        "card_url": "transport:jsonrpc",
        "auth_method": "transport:jsonrpc",
        "credential_ref": "auth_method:bearer,header",
        "auth_header": "auth_method:header",
        "options": "transport:jsonrpc,mock",
        "declared_skills": "transport:mailbox",
    }

    def __init__(self, *args, initial_agent=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].required = False  # an internal agent's card takes the agent's name
        self.fields["agent"].label_from_instance = lambda agent: f"{agent.name} · {agent.agent_id}"
        agents = self.fields["agent"].queryset.order_by("name")
        if self.creating:
            self.fields["agent"].queryset = agents.filter(a2a_card__isnull=True)
            if initial_agent is not None:
                self.initial.setdefault("origin", CardOrigin.INTERNAL)
                self.initial.setdefault("agent", initial_agent.pk)
        else:
            # Senders and delegation graphs name it by its address.
            for name in ("origin", "agent", "slug"):
                self.fields[name].disabled = True
            self.fields["slug"].help_text = "Where other agents reach it. It cannot change."

    def clean_credential_ref(self):
        return clean_credential_ref(self.cleaned_data.get("credential_ref"))

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        agent = self.cleaned_data.get("agent")
        if not name and self.cleaned_data.get("origin") == CardOrigin.INTERNAL and agent is not None:
            name = agent.name
        if not name:
            raise ValidationError("Name the agent.")
        return name

    def clean(self):
        cleaned = super().clean()
        if not self.creating:
            return cleaned
        origin, agent = cleaned.get("origin"), cleaned.get("agent")
        if origin == CardOrigin.INTERNAL:
            if agent is None:
                self.add_error("agent", "Choose the agent this card publishes.")
                return cleaned
            cleaned["slug"] = self.instance.slug = A2AAgentCard.clean_slug(agent.agent_id)
            if not cleaned.get("description"):
                cleaned["description"] = agent.business_purpose or agent.description
        else:
            cleaned["agent"] = None
            cleaned["slug"] = self.instance.slug = A2AAgentCard.clean_slug(cleaned.get("slug") or cleaned.get("name") or "")
        if A2AAgentCard.objects.filter(organization=self.organization, slug=cleaned["slug"]).exists():
            self.add_error(
                "agent" if origin == CardOrigin.INTERNAL else ("slug" if self.data.get("slug") else "name"),
                f"Another agent is already published at “{cleaned['slug']}”.",
            )
        return cleaned


class A2AContractForm(OrganizationModelForm):
    allowed_skills = ChoiceListField(
        label="Skills", help_text="Leave every box empty for all of its enabled skills, now and later.",
    )
    expires_at = forms.DateTimeField(
        label="Expires", required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"],
        help_text="Optional. An expired contract stops working on its own.",
    )

    class Meta:
        model = A2AContract
        fields = ["sender", "allowed_skills", "max_data_classification", "max_messages_per_hour", "expires_at", "purpose"]
        labels = {
            "sender": "Sending agent", "max_data_classification": "Data ceiling",
            "max_messages_per_hour": "Messages per hour",
        }
        help_texts = {
            "max_data_classification": "The most sensitive data the sender may send along this contract.",
            "max_messages_per_hour": "Optional. Messages beyond it in any hour are refused.",
            "purpose": "Why this agent needs to reach the other, for whoever reviews the contract later.",
        }

    sections = (("Contract", "", [
        "sender", "allowed_skills", "max_data_classification", "max_messages_per_hour", "expires_at", "purpose",
    ]),)
    compact_fields = ("sender", "max_data_classification", "max_messages_per_hour", "expires_at")

    def __init__(self, *args, card=None, **kwargs):
        self.card = card
        super().__init__(*args, **kwargs)
        skills = card.skills.exclude(status=SkillStatus.REMOVED).order_by("skill_id")
        self.fields["allowed_skills"].choices = [
            (skill.skill_id, f"{skill.skill_id} · {skill.get_status_display().lower()}") for skill in skills
        ]
        self.fields["allowed_skills"].include(self.instance.allowed_skills)

        clearance = card.data_classification
        self.fields["max_data_classification"].choices = [("", f"Its clearance: {clearance}")] + [
            (value, label) for value, label in DataClassification.choices
            if sensitivity(value) <= sensitivity(clearance)
        ]

        senders = self.fields["sender"].queryset.order_by("name")
        if card.agent_id:
            senders = senders.exclude(pk=card.agent_id)
        if self.creating:
            senders = senders.exclude(a2a_contracts__recipient=card)
        else:
            self.fields["sender"].disabled = True
        self.fields["sender"].queryset = senders
        self.fields["sender"].label_from_instance = lambda agent: f"{agent.name} · {agent.agent_id}"

    def clean_sender(self):
        sender = self.cleaned_data.get("sender")
        if self.creating and sender and A2AContract.objects.filter(sender=sender, recipient=self.card).exists():
            raise ValidationError(f"{sender} already holds a contract for this agent; edit that one.")
        return sender
