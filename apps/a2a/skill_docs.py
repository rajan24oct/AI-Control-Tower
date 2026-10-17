"""A catalog skill as a SKILL.md file another agent's developer can read.

The file is the skill as governance sees it -- what the agent advertises, the
review decision, and how to reach it through the A2A Gateway -- in the
`SKILL.md` shape agent frameworks load: YAML front matter, then Markdown.
Strings in the front matter are written as JSON, which YAML reads as quoted
scalars, so a description with a colon or a quote cannot break the header.
"""

from __future__ import annotations

import io
import json
import zipfile

from django.utils import timezone
from django.utils.text import slugify

from apps.a2a.models import A2ASkill, SkillStatus


def _scalar(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _example_call(skill: A2ASkill, address: str) -> str:
    example = skill.examples[0] if skill.examples else f"<what you need from {skill.label}>"
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "messageId": "<unique id>",
                "parts": [{"kind": "text", "text": example}],
                "metadata": {"io.aicontroltower/skill": skill.skill_id},
            }
        },
    }
    return (
        f"curl -X POST {address} \\\n"
        '  -H "Authorization: Bearer $A2A_KEY" \\\n'
        '  -H "X-Agent-Id: <your agent id>" \\\n'
        '  -H "Content-Type: application/json" \\\n'
        f"  -d '{json.dumps(body, indent=2)}'"
    )


def skill_markdown(skill: A2ASkill, address: str) -> str:
    """One skill as SKILL.md. `address` is the absolute A2A endpoint of its agent."""
    card = skill.card
    front = [
        "---",
        f"name: {_scalar(skill.skill_id)}",
        f"description: {_scalar(skill.description or skill.label)}",
        "metadata:",
        f"  title: {_scalar(skill.label)}",
        f"  qualified_name: {_scalar(skill.qualified_name)}",
        f"  agent: {_scalar(card.name)}",
        f"  agent_address: {_scalar(address)}",
        f"  external: {'true' if card.is_external else 'false'}",
        f"  status: {_scalar(skill.status)}",
        f"  risk_level: {_scalar(skill.risk_level)}",
        f"  tags: {_scalar(list(skill.tags or []))}",
        f"  input_modes: {_scalar(list(skill.input_modes or []))}",
        f"  output_modes: {_scalar(list(skill.output_modes or []))}",
        f"  definition_hash: {_scalar(skill.definition_hash)}",
        "---",
    ]

    reviewer = (skill.reviewed_by.get_full_name() or skill.reviewed_by.email) if skill.reviewed_by else ""
    if skill.status == SkillStatus.ENABLED:
        verdict = "Enabled: the A2A Gateway delivers calls to this skill, within the contracts that grant it."
    elif skill.status == SkillStatus.PENDING_REVIEW:
        verdict = "Awaiting review: the A2A Gateway refuses calls to this skill until governance enables it."
    elif skill.status == SkillStatus.DISABLED:
        verdict = "Disabled: governance reviewed this skill and the A2A Gateway refuses calls to it."
    else:
        verdict = "No longer advertised: the agent's card stopped listing this skill."

    body = [
        "",
        f"# {skill.label}",
        "",
        skill.description or "_The agent does not describe this skill._",
        "",
        "## Governance",
        "",
        f"- **Agent:** {card.name}{' (external)' if card.is_external else ''}",
        f"- **Risk:** {skill.get_risk_level_display()}",
        f"- **Status:** {verdict}",
    ]
    if skill.changed_since_review:
        body.append("- **Changed since review:** the agent changed this skill's definition after it was reviewed.")
    if reviewer:
        stamp = f" on {skill.reviewed_at:%d %b %Y}" if skill.reviewed_at else ""
        body.append(f"- **Reviewed by:** {reviewer}{stamp}")
    if skill.review_note:
        body.append(f"- **Review note:** {skill.review_note}")

    if skill.tags or skill.input_modes or skill.output_modes:
        body += ["", "## Interface", ""]
        if skill.tags:
            body.append(f"- **Tags:** {', '.join(skill.tags)}")
        if skill.input_modes:
            body.append(f"- **Accepts:** {', '.join(skill.input_modes)}")
        if skill.output_modes:
            body.append(f"- **Returns:** {', '.join(skill.output_modes)}")

    if skill.examples:
        body += ["", "## Examples", ""]
        body += [f"- {example}" for example in skill.examples]

    body += [
        "",
        "## Calling it",
        "",
        "Send an A2A `message/send` to the agent's address through the A2A Gateway, naming the skill in the "
        "message metadata. The gateway checks the contract between your agent and this one, the data "
        "classification of the message and the skill's review status before delivering it.",
        "",
        "```bash",
        _example_call(skill, address),
        "```",
        "",
        f"_Exported from the A2A Skill Catalog on {timezone.now():%d %b %Y %H:%M} UTC._",
        "",
    ]
    return "\n".join(front + body)


def skills_archive(skills, address_for) -> bytes:
    """Many skills as a zip of `<agent>/<skill>/SKILL.md`, one folder per skill."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for skill in skills:
            archive.writestr(
                # Skill ids come from agent cards, so they are slugged before use as a path.
                f"skills/{skill.card.slug}/{slugify(skill.skill_id) or skill.pk}/SKILL.md",
                skill_markdown(skill, address_for(skill.card)),
            )
    return buffer.getvalue()
