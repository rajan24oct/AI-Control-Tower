"""What a message carries: its text, its size and how sensitive it is.

The gateway does not read messages for meaning. To hold a message to a
contract it needs three facts: how big it is, how sensitive its sender says it
is, and whether it plainly contains personal data -- in which case it is
treated as at least confidential, whatever was declared. A sender that declares
nothing is taken to send data as sensitive as its own classification: the
cautious reading of silence.

A clean scan means nothing matched, never that no personal data is present
(see apps.executions.pii). The scan can only raise a classification, never
lower one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from apps.a2a.models import sensitivity
from apps.core.constants import DataClassification
from apps.executions import pii
from integrations.base.a2a import parts_text

#: Personal data found in a message makes it at least this sensitive.
PERSONAL_DATA_FLOOR = DataClassification.CONFIDENTIAL


@dataclass
class Content:
    text: str = ""
    size: int = 0
    declared: str = ""
    classification: str = ""
    personal_data: list[dict] = field(default_factory=list)

    @property
    def raised(self) -> bool:
        """Personal data made it more sensitive than the sender said."""
        return self.classification != (self.declared or self.classification)

    @property
    def personal_data_kinds(self) -> list[str]:
        return [item["key"] for item in self.personal_data]

    @property
    def personal_data_labels(self) -> str:
        return ", ".join(item["label"].lower() for item in self.personal_data)


def inspect(message: dict, *, declared: str = "", fallback: str = "") -> Content:
    """The facts the gateway holds a message to."""
    text = parts_text(message.get("parts"))
    summary = pii.summarise(pii.scan(text))
    base = declared if declared in DataClassification.values else (fallback or DataClassification.INTERNAL)
    classification = base
    if summary and sensitivity(base) < sensitivity(PERSONAL_DATA_FLOOR):
        classification = PERSONAL_DATA_FLOOR
    return Content(
        text=text,
        size=len(json.dumps(message, default=str).encode("utf-8")),
        declared=declared if declared in DataClassification.values else "",
        classification=classification,
        personal_data=summary,
    )
