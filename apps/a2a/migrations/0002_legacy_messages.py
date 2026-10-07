"""Keep the agent messages that already flowed flowing, under the A2A Gateway.

Before the A2A Gateway, any agent could message any other through the mailbox.
From now on a message reaches only a published, approved agent -- and a
restricted one only from a sender holding a contract. So that nothing that
worked stops working on upgrade, every pair that had already exchanged a
message gets exactly what it needs: the recipient is published (approved,
restricted, cleared for the most sensitive data its senders handle, since a
sender that declares nothing is read as sending its own classification) and
each of its senders holds a contract for it.

Nothing new is opened: only the pairs that were already talking, and each
contract says where it came from so governance can review it.
"""

from django.db import migrations
from django.utils import timezone
from django.utils.text import slugify

SENSITIVITY = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}
PURPOSE = "Recorded from the agent messages sent before the A2A Gateway existed. Review it."


def _free_slug(model, organization_id, slug):
    candidate, number = slug, 2
    while model.objects.filter(organization_id=organization_id, slug=candidate).exists():
        candidate, number = f"{slug}-{number}", number + 1
    return candidate


def forwards(apps, schema_editor):
    GatewayMessage = apps.get_model("gateway", "GatewayMessage")
    A2AAgentCard = apps.get_model("a2a", "A2AAgentCard")
    A2AContract = apps.get_model("a2a", "A2AContract")
    AIAgent = apps.get_model("agents", "AIAgent")

    senders_of: dict = {}
    pairs = (
        GatewayMessage.objects.exclude(status="blocked")
        .values_list("organization_id", "sender_id", "recipient_id")
        .distinct()
    )
    for organization_id, sender_id, recipient_id in pairs:
        if sender_id != recipient_id:
            senders_of.setdefault((organization_id, recipient_id), set()).add(sender_id)
    if not senders_of:
        return

    wanted = {pk for (_, recipient_id), senders in senders_of.items() for pk in (recipient_id, *senders)}
    agents = AIAgent.objects.in_bulk(wanted)
    now = timezone.now()
    for (organization_id, recipient_id), sender_ids in senders_of.items():
        recipient = agents.get(recipient_id)
        if recipient is None:
            continue
        card = A2AAgentCard.objects.filter(agent_id=recipient_id).first()
        if card is None:
            clearance = max(
                [recipient.data_classification, *(agents[pk].data_classification for pk in sender_ids if pk in agents)],
                key=lambda value: SENSITIVITY.get(value, 0),
            )
            card = A2AAgentCard.objects.create(
                organization_id=organization_id,
                origin="internal",
                agent_id=recipient_id,
                name=recipient.name,
                slug=_free_slug(A2AAgentCard, organization_id, slugify(recipient.agent_id)[:200].strip("-_") or "agent"),
                description=recipient.business_purpose or recipient.description,
                transport="mailbox",
                data_classification=clearance,
                risk_level=recipient.risk_level,
                is_approved=True,
                approved_at=now,
                access_mode="restricted",
                owner_id=recipient.owner_id,
                team_id=recipient.team_id,
                status="active",
            )
        for sender_id in sender_ids:
            if sender_id in agents:
                A2AContract.objects.get_or_create(
                    sender_id=sender_id, recipient=card,
                    defaults={"organization_id": organization_id, "purpose": PURPOSE},
                )


class Migration(migrations.Migration):
    dependencies = [
        ("a2a", "0001_initial"),
        ("agents", "0003_aiagent_kill_switch_triggers"),
        ("gateway", "0001_initial"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
