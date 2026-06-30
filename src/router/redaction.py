from __future__ import annotations

import copy
import re
from typing import Any

ENV_SECRET_RE = re.compile(r"\b([A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|MASTER_KEY|SALT_KEY))=([^\s]+)")
SK_SECRET_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
OLLAMA_SECRET_RE = re.compile(r"\b[0-9a-f]{32}\.[A-Za-z0-9_-]{8,}\b")


def redact_text(text: str) -> str:
    redacted = ENV_SECRET_RE.sub(r"\1=[REDACTED]", text)
    redacted = SK_SECRET_RE.sub("[REDACTED]", redacted)
    return OLLAMA_SECRET_RE.sub("[REDACTED]", redacted)


def redact_payload(payload: Any) -> Any:
    return _redact(copy.deepcopy(payload))


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    return value
