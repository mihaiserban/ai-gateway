from __future__ import annotations

import re
from typing import Any

CODE_SIGNALS = (
    "refactor",
    "implement",
    "stack trace",
    "traceback",
    "diff",
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".go",
    ".rs",
    "src/",
    "tests/",
)

REASONING_SIGNALS = (
    "analyze",
    "architecture",
    "design",
    "why",
    "race condition",
    "root cause",
    "debug",
    "explain",
)


def classify_request(
    request: dict[str, Any],
    *,
    code_signals: tuple[str, ...] = CODE_SIGNALS,
    reasoning_signals: tuple[str, ...] = REASONING_SIGNALS,
) -> str:
    if _has_image_content(request.get("messages", [])):
        return "vision"

    text = _message_text(request.get("messages", [])).lower()
    if any(_contains_signal(text, signal) for signal in code_signals):
        return "opencodego-fast"
    if any(_contains_signal(text, signal) for signal in reasoning_signals):
        return "deepseek-pro"
    return "fast"


def _has_image_content(messages: Any) -> bool:
    if not isinstance(messages, list):
        return False

    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") in {"image_url", "input_image"}:
                    return True
    return False


def _message_text(messages: Any) -> str:
    parts: list[str] = []
    if not isinstance(messages, list):
        return ""

    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text") or part.get("content")
                    if isinstance(text, str):
                        parts.append(text)
    return "\n".join(parts)


def _contains_signal(text: str, signal: str) -> bool:
    signal = signal.strip().lower()
    if not signal:
        return False
    if signal[0].isalnum() and signal[-1].isalnum():
        return re.search(rf"(?<!\w){re.escape(signal)}(?!\w)", text) is not None
    return signal in text
