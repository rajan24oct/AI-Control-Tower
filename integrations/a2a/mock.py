"""Mock A2A agent adapter.

Makes the A2A Gateway demonstrable with no remote agent deployed, the way the
mock MCP server does for tools. The card comes from the registry record's
options (`card`, written in A2A's own wire shape) or a small default, and
answers are derived from the message itself, so demos and tests reproduce
exactly.

Options:
  simulate_outage: true     every request fails the way an unreachable agent would
  reply_state: "working"    answer with a task still in progress (default: completed)
  get_state: "completed"    what `tasks/get` reports afterwards (default: completed)
  reject_skills: [...]      the agent itself rejects work asked of these skills
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from integrations.base.a2a import (
    PROTOCOL_VERSION,
    A2ACardInfo,
    A2AProtocolError,
    A2ATaskResult,
    A2ATransportError,
    BaseA2AAdapter,
    parts_text,
)
from integrations.registry import register_a2a

SKILL_META_KEY = "io.aicontroltower/skill"

DEFAULT_CARD = {
    "name": "Mock A2A Agent",
    "description": "Answers deterministically from the message it is sent.",
    "version": "1.0.0",
    "defaultInputModes": ["text/plain", "application/json"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {"id": "summarize", "name": "Summarize", "description": "Summarize the text it is sent.",
         "tags": ["text"]},
        {"id": "classify", "name": "Classify", "description": "Label the text it is sent with a category.",
         "tags": ["text", "classification"]},
    ],
}


@register_a2a("mock")
class MockA2AAdapter(BaseA2AAdapter):
    adapter_key = "mock"
    display_name = "Mock / Demo A2A Agent"

    def _available(self) -> None:
        if self.config.option("simulate_outage", False):
            raise A2ATransportError(
                f"{self.config.name or 'Mock agent'} is not responding (simulated outage)."
            )

    def get_card(self) -> A2ACardInfo:
        self._available()
        card = {**DEFAULT_CARD, **(self.config.option("card") or {})}
        card.setdefault("name", self.config.name)
        card["protocolVersion"] = PROTOCOL_VERSION
        card["url"] = self.config.endpoint or "mock://a2a"
        card["capabilities"] = {"streaming": False, "pushNotifications": False}
        return A2ACardInfo.from_wire(card)

    @staticmethod
    def _task_id(message: dict[str, Any]) -> str:
        seed = str(message.get("messageId") or json.dumps(message, sort_keys=True, default=str))
        return "mock-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _artifact(task_id: str, skill: str, text: str) -> dict[str, Any]:
        digest = hashlib.sha256(f"{skill}:{text}".encode("utf-8")).hexdigest()[:10]
        return {
            "artifactId": f"{task_id}-result",
            "name": "result",
            "parts": [{"kind": "text", "text": f"[mock:{skill or 'reply'}] result {digest} for {text[:120]}"}],
        }

    def send_message(self, message, *, metadata=None, configuration=None) -> A2ATaskResult:
        self._available()
        if not isinstance(message, dict) or not message.get("parts"):
            raise A2AProtocolError(-32602, "A message needs at least one part.")
        skill = str((message.get("metadata") or {}).get(SKILL_META_KEY) or (metadata or {}).get(SKILL_META_KEY) or "")
        task_id = self._task_id(message)
        context_id = str(message.get("contextId") or f"ctx-{task_id}")
        if skill and skill in (self.config.option("reject_skills") or []):
            return A2ATaskResult(
                task_id=task_id, context_id=context_id, state="rejected",
                status_message={"kind": "message", "role": "agent", "messageId": f"{task_id}-status",
                                "parts": [{"kind": "text", "text": "Declined by the agent."}]},
            )
        state = str(self.config.option("reply_state") or "completed")
        artifacts = [self._artifact(task_id, skill, parts_text(message.get("parts")))] if state == "completed" else []
        return A2ATaskResult(task_id=task_id, context_id=context_id, state=state, artifacts=artifacts)

    def get_task(self, task_id: str) -> A2ATaskResult:
        self._available()
        state = str(self.config.option("get_state") or "completed")
        artifacts = [self._artifact(task_id, "", "the finished work")] if state == "completed" else []
        return A2ATaskResult(task_id=task_id, state=state, artifacts=artifacts)

    def cancel_task(self, task_id: str) -> A2ATaskResult:
        self._available()
        return A2ATaskResult(task_id=task_id, state="canceled")
