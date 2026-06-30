from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("litellm.provider")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_handler)


def _resolved_provider_model(obj: Any) -> str | None:
    """Extract the actual provider/model string from a litellm response object."""
    candidate: Any = None
    if isinstance(obj, dict):
        candidate = obj.get("model")
    elif obj is not None:
        try:
            candidate = obj.model
        except AttributeError:
            candidate = None
    return candidate if isinstance(candidate, str) else None


class ProviderModelLogger:
    """LiteLLM callback logger that emits the resolved provider/model per request."""

    @staticmethod
    async def async_post_call_success_hook(user_api_key_dict: Any, response: Any, **kwargs: Any) -> None:
        provider_model = _resolved_provider_model(response)
        if provider_model is None:
            return
        logger.info("provider_model=%s status=success", provider_model)

    @staticmethod
    async def async_post_call_failure_hook(user_api_key_dict: Any, original_exception: Any, **kwargs: Any) -> None:
        # Failure hooks don't always receive the resolved response, so we try to
        # surface what we can without logging the exception details.
        provider_model = _resolved_provider_model(original_exception)
        if provider_model is None:
            return
        logger.info("provider_model=%s status=failure", provider_model)
