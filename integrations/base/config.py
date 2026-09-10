"""Configuration handed to an adapter instance.

Deliberately a plain dataclass: the `integrations` package stays importable
without Django so adapters can be unit-tested and reused by external workers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from integrations.base.exceptions import CredentialsUnavailable


@dataclass
class AdapterConfig:
    """Everything an adapter needs, minus the secret itself.

    `credential_ref` names an environment variable or secret-manager key. The
    raw credential is never stored in the database and never lives on a model
    instance (section 11).
    """

    key: str = ""
    name: str = ""
    endpoint: str = ""
    region: str = ""
    deployment: str = ""
    credential_ref: str = ""
    options: dict[str, Any] = field(default_factory=dict)

    def resolve_credential(self, *, required: bool = True) -> str | None:
        if not self.credential_ref:
            if required:
                raise CredentialsUnavailable(
                    f"Adapter '{self.key}' has no credential_ref configured."
                )
            return None

        secret = os.environ.get(self.credential_ref)
        if not secret:
            if required:
                raise CredentialsUnavailable(
                    f"Environment variable '{self.credential_ref}' is unset or empty."
                )
            return None
        return secret

    def has_credential(self) -> bool:
        return bool(self.credential_ref and os.environ.get(self.credential_ref))

    def option(self, name: str, default: Any = None) -> Any:
        return self.options.get(name, default)
