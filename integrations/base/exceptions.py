"""Adapter-layer exceptions.

The Control Tower catches these instead of vendor-specific SDK errors, which
keeps provider libraries from leaking into application code.
"""

from __future__ import annotations


class AdapterError(Exception):
    """Base class for every adapter failure."""


class AdapterNotFound(AdapterError):
    """No adapter is registered under the requested key."""


class AdapterConfigurationError(AdapterError):
    """The adapter is registered but not usable as configured."""


class AdapterDependencyMissing(AdapterConfigurationError):
    """An optional vendor SDK is required but not installed."""


class CredentialsUnavailable(AdapterConfigurationError):
    """The referenced secret could not be resolved from the environment."""


class CapabilityNotSupported(AdapterError):
    """The operation requires a capability this adapter does not advertise."""

    def __init__(self, adapter_key: str, capability: str):
        self.adapter_key = adapter_key
        self.capability = capability
        super().__init__(f"Adapter '{adapter_key}' does not support '{capability}'.")


class ProviderUnavailable(AdapterError):
    """The upstream provider could not be reached or returned a fatal error."""


class ExecutionNotFound(AdapterError):
    """The referenced remote execution does not exist."""
