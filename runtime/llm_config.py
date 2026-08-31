"""One place to decide which LLM this project talks to.

Any provider with an OpenAI-compatible chat-completions endpoint works —
OpenAI, DeepSeek, Moonshot, Zhipu, Qwen, Groq, Together, OpenRouter, or a
local Ollama / LM Studio. Point ``LLM_BASE_URL`` at it and name the model.

    LLM_API_KEY    the key (``OPENAI_API_KEY`` also accepted)
    LLM_BASE_URL   endpoint; unset means OpenAI's own
    LLM_MODEL      model id; defaults to gpt-4o-mini

Cost reporting only knows OpenAI's published prices. For anything else, set
``LLM_PRICE_INPUT`` / ``LLM_PRICE_OUTPUT`` (USD per 1M tokens) to get real
numbers, or accept that the reported cost is unknown.
"""

import os
from typing import Optional

DEFAULT_MODEL = "gpt-4o-mini"

# USD per 1M tokens.
PRICING = {
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4-turbo": {"input": 10.00, "output": 30.00},
}


def api_key() -> str:
    return (
        os.environ.get("LLM_API_KEY", "").strip()
        or os.environ.get("OPENAI_API_KEY", "").strip()
    )


def base_url() -> Optional[str]:
    url = (
        os.environ.get("LLM_BASE_URL", "").strip()
        or os.environ.get("OPENAI_BASE_URL", "").strip()
    )
    return url or None


def model() -> str:
    return os.environ.get("LLM_MODEL", "").strip() or DEFAULT_MODEL


def provider_label() -> str:
    """Something human-readable for logs, e.g. 'deepseek-chat @ api.deepseek.com'."""
    url = base_url()
    if not url:
        return f"{model()} @ OpenAI"
    host = url.split("//", 1)[-1].split("/", 1)[0]
    return f"{model()} @ {host}"


def pricing_for(model_id: str) -> Optional[dict]:
    """Published prices, an explicit override, or None when genuinely unknown."""
    override_in = os.environ.get("LLM_PRICE_INPUT", "").strip()
    override_out = os.environ.get("LLM_PRICE_OUTPUT", "").strip()
    if override_in and override_out:
        try:
            return {"input": float(override_in), "output": float(override_out)}
        except ValueError:
            pass
    return PRICING.get(model_id)


def client(**kwargs):
    """An OpenAI-SDK client pointed at whichever provider is configured."""
    from openai import OpenAI

    key = kwargs.pop("api_key", None) or api_key()
    if not key:
        raise RuntimeError(
            "No API key. Put LLM_API_KEY (or OPENAI_API_KEY) in .env — "
            "see .env.example for the providers this works with."
        )
    url = kwargs.pop("base_url", None) or base_url()
    if url:
        kwargs["base_url"] = url
    return OpenAI(api_key=key, **kwargs)
