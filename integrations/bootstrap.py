"""Adapter discovery.

Importing an adapter module runs its `@register_*` decorator. This is the one
place that knows which adapter modules exist; adding a vendor means adding a
line here, and nothing else in the platform changes.

Called from `apps.integrations.apps.IntegrationsConfig.ready()`.
"""

from __future__ import annotations

import importlib
import logging

logger = logging.getLogger(__name__)

ADAPTER_MODULES = [
    "integrations.providers.mock",
    "integrations.providers.openai",
    "integrations.providers.generic_rest",
    "integrations.agents.mock",
    "integrations.webhooks.generic",
]

_loaded = False


def load_adapters(force: bool = False) -> None:
    global _loaded
    if _loaded and not force:
        return

    for module_path in ADAPTER_MODULES:
        try:
            importlib.import_module(module_path)
        except Exception:
            # A broken optional adapter must not prevent the Control Tower from
            # starting -- the rest of the platform stays available.
            logger.exception("Failed to load adapter module %s", module_path)

    _loaded = True
