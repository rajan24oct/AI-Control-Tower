"""Guardrail detectors: what to look for in text going to, or coming from, a model.

Each detector returns hits with their position, so a policy can block on them,
redact exactly those spans, or just record them. Personal data reuses the
platform's own scanner (`apps.executions.pii`), so a guardrail and a prompt
view agree on what counts as personal data.

These are pattern detectors: fast, explainable, and cheap enough to run on
every request. They catch the common forms; they are not a guarantee, and a
determined attacker can phrase around patterns. Treat them as one layer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from apps.executions import pii


@dataclass(frozen=True)
class Hit:
    detector: str
    kind: str
    label: str
    start: int
    end: int
    weight: float = 1.0


# -- Prompt injection ---------------------------------------------------------------------

#: (kind, label, pattern, weight). Weights combine as independent signals.
INJECTION_PATTERNS = [
    ("override_instructions", "Asks to ignore earlier instructions",
     r"\b(?:ignore|disregard|forget|skip)\b[^.\n]{0,40}?\b(?:previous|prior|above|earlier|all|any|your)\b[^.\n]{0,25}?"
     r"\b(?:instructions?|prompts?|rules?|directions?|guidelines?)\b", 0.9),
    ("reveal_system_prompt", "Asks for the system prompt or hidden instructions",
     r"\b(?:reveal|show|print|repeat|output|tell me|what (?:is|are))\b[^.\n]{0,30}?"
     r"\b(?:system prompt|hidden (?:instructions?|prompt)|your (?:instructions?|initial prompt|rules))\b", 0.8),
    ("role_override", "Tries to give the model a new, unrestricted role",
     r"\byou are now\b|\bact as (?:an? )?(?:unfiltered|unrestricted|jailbroken|uncensored)\b|\bdeveloper mode\b"
     r"|\bdo anything now\b|\bDAN\b", 0.7),
    ("disable_safety", "Asks to bypass safety, policy or filters",
     r"\b(?:override|bypass|disable|turn off|ignore)\b[^.\n]{0,30}?\b(?:safety|guardrails?|polic(?:y|ies)|filters?|"
     r"restrictions?|content rules)\b", 0.8),
    ("fake_system_message", "Imitates a system or administrator message",
     r"(?:begin|end)\s+(?:system|admin(?:istrator)?)\s+(?:prompt|message|instructions?)|<\s*/?\s*system\s*>"
     r"|\[\s*(?:system|admin)\s*\]|^\s*system\s*:", 0.6),
    ("new_instructions", "Introduces new instructions mid-content",
     r"\bnew instructions?\s*:|\bfrom now on,? you (?:will|must|should)\b", 0.5),
    ("exfiltration", "Asks to send data to an outside address",
     r"\b(?:send|post|upload|forward|exfiltrate)\b[^.\n]{0,40}?\b(?:to|at)\b[^.\n]{0,10}?(?:https?://|\S+@\S+\.\w+)", 0.6),
]
_INJECTION = [(kind, label, re.compile(pattern, re.IGNORECASE | re.MULTILINE), weight)
              for kind, label, pattern, weight in INJECTION_PATTERNS]
DEFAULT_INJECTION_THRESHOLD = 0.6


def injection_score(hits: list[Hit]) -> float:
    """Independent signals combined: 1 - product(1 - weight) over distinct kinds."""
    remaining = 1.0
    for weight in {hit.kind: hit.weight for hit in hits}.values():
        remaining *= 1 - weight
    return round(1 - remaining, 3)


def detect_injection(text: str, config: dict) -> list[Hit]:
    hits = [Hit("prompt_injection", kind, label, m.start(), m.end(), weight)
            for kind, label, pattern, weight in _INJECTION for m in pattern.finditer(text)]
    threshold = float(config.get("threshold", DEFAULT_INJECTION_THRESHOLD))
    return hits if hits and injection_score(hits) >= threshold else []


# -- Personal data ------------------------------------------------------------------------

PII_KINDS = {kind.key: kind.label for kind in pii.KINDS}


def detect_pii(text: str, config: dict) -> list[Hit]:
    wanted = set(config.get("kinds") or PII_KINDS)
    return [Hit("pii", f.kind, f.label, f.start, f.end) for f in pii.scan(text) if f.kind in wanted]


# -- Secrets ----------------------------------------------------------------------------------

SECRET_PATTERNS = [
    ("openai_key", "OpenAI API key", r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    ("anthropic_key", "Anthropic API key", r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
    ("aws_access_key", "AWS access key ID", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("github_token", "GitHub token", r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    ("slack_token", "Slack token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    ("private_key", "Private key", r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    ("jwt", "JSON Web Token", r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    ("platform_key", "Control Tower API key", r"\bact_[A-Za-z0-9]{6,}_[A-Za-z0-9_-]{16,}\b"),
    ("password", "Password in text", r"\b(?:password|passwd|pwd|secret)\s*[:=]\s*\S{6,}"),
]
_SECRETS = [(kind, label, re.compile(pattern, re.IGNORECASE if kind == "password" else 0))
            for kind, label, pattern in SECRET_PATTERNS]


def detect_secrets(text: str, config: dict) -> list[Hit]:
    return [Hit("secrets", kind, label, m.start(), m.end()) for kind, label, pattern in _SECRETS
            for m in pattern.finditer(text)]


# -- Restricted topics ----------------------------------------------------------------------


def detect_topics(text: str, config: dict) -> list[Hit]:
    hits = []
    for term in config.get("terms") or []:
        term = str(term).strip()
        if not term:
            continue
        for m in re.finditer(rf"\b{re.escape(term)}\b", text, re.IGNORECASE):
            hits.append(Hit("denied_topics", term.lower(), f"Restricted topic: {term}", m.start(), m.end()))
    return hits


DETECTORS = {
    "prompt_injection": detect_injection,
    "pii": detect_pii,
    "secrets": detect_secrets,
    "denied_topics": detect_topics,
}


def detect(detector: str, text: str, config: dict | None = None) -> list[Hit]:
    if not text or detector not in DETECTORS:
        return []
    return DETECTORS[detector](text, config or {})


def redact(text: str, hits: list[Hit]) -> str:
    """Replace each hit with a labelled placeholder, merging overlaps."""
    spans = sorted(((hit.start, hit.end, hit.kind) for hit in hits), key=lambda span: span[0])
    merged: list[list] = []
    for start, end, kind in spans:
        if merged and start < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end, kind])
    for start, end, kind in reversed(merged):
        text = f"{text[:start]}[REDACTED:{kind.upper()}]{text[end:]}"
    return text
