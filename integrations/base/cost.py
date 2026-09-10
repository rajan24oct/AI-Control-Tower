"""Cost reporting contract.

Some providers expose authoritative billing APIs. Where they do, a cost adapter
reconciles the Control Tower's derived costs against the vendor's own numbers.
"""

from __future__ import annotations

import abc
from datetime import datetime

from integrations.base.config import AdapterConfig
from integrations.base.types import UsageRecord


class BaseCostAdapter(abc.ABC):
    adapter_key: str = ""
    display_name: str = ""
    currency: str = "USD"

    def __init__(self, config: AdapterConfig | None = None):
        self.config = config or AdapterConfig(key=self.adapter_key)

    @abc.abstractmethod
    def get_usage_records(self, start: datetime, end: datetime) -> list[UsageRecord]:
        """Authoritative usage and spend for a period, as billed by the vendor."""

    def supports_reconciliation(self) -> bool:
        return True
