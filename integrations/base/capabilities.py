"""Capability vocabulary for capability-based dispatch (section 2).

Adapters advertise what they can do. Application code asks before it acts,
instead of hard-coding assumptions about a particular vendor.
"""

from __future__ import annotations

from enum import StrEnum


class Capability(StrEnum):
    SUPPORTS_CHAT = "supports_chat"
    SUPPORTS_STREAMING = "supports_streaming"
    SUPPORTS_EMBEDDINGS = "supports_embeddings"
    SUPPORTS_VISION = "supports_vision"
    SUPPORTS_TOOL_CALLING = "supports_tool_calling"
    SUPPORTS_STRUCTURED_OUTPUT = "supports_structured_output"
    SUPPORTS_REASONING = "supports_reasoning"
    SUPPORTS_AUDIO = "supports_audio"
    SUPPORTS_IMAGE_GENERATION = "supports_image_generation"
    SUPPORTS_USAGE_METRICS = "supports_usage_metrics"
    SUPPORTS_NATIVE_AGENTS = "supports_native_agents"
    SUPPORTS_COST_REPORTING = "supports_cost_reporting"
    SUPPORTS_HEALTH_CHECK = "supports_health_check"


#: Convenience grouping used by registry UIs and seed data.
CHAT_BASELINE = frozenset(
    {
        Capability.SUPPORTS_CHAT,
        Capability.SUPPORTS_STREAMING,
        Capability.SUPPORTS_USAGE_METRICS,
        Capability.SUPPORTS_HEALTH_CHECK,
    }
)


def normalize(capabilities) -> set[str]:
    """Accept enums or raw strings, return a set of plain strings."""
    return {str(c) for c in (capabilities or [])}
