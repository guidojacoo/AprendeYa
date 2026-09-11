"""A small, provider-agnostic LLM client (stdlib only).

The upstream project never called an LLM itself: the brain was Hermes, a separate
runtime you installed on a VPS. That cannot follow us onto Vercel, so the brain
has to become a function call — and since we are paying nothing, it must be able
to point at whichever free tier is open at the time.

Three wire formats cover everything worth using:

  openai      /chat/completions   Groq, OpenRouter, OpenAI, DeepSeek, Together…
  anthropic   /v1/messages        Claude
  gemini      :generateContent    Google AI Studio

Set LLM_PROVIDER / LLM_API_KEY / LLM_MODEL and the rest is defaults. Set nothing
and the bot stays fully deterministic — which is the default, and costs zero.
"""

import json
import urllib.error
import urllib.request

from .. import config

# provider -> (wire format, base url, default model)
# The default models are the free-tier-friendly choice for each provider; override
# with LLM_MODEL whenever a provider retires one.
PROVIDERS = {
    "groq": ("openai", "https://api.groq.com/openai/v1",
             "llama-3.3-70b-versatile"),
    "openrouter": ("openai", "https://openrouter.ai/api/v1",
                   "meta-llama/llama-3.3-70b-instruct:free"),
    "openai": ("openai", "https://api.openai.com/v1", "gpt-4o-mini"),
    "deepseek": ("openai", "https://api.deepseek.com/v1", "deepseek-chat"),
    "together": ("openai", "https://api.together.xyz/v1",
                 "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
    "anthropic": ("anthropic", "https://api.anthropic.com", "claude-sonnet-5"),
    "gemini": ("gemini", "https://generativelanguage.googleapis.com/v1beta",
               "gemini-2.0-flash"),
}


class LLMError(Exception):
    pass


def enabled():
    return (config.LLM_PROVIDER not in ("", "none", "off", "disabled")
            and bool(config.LLM_API_KEY))


def describe():
    """What the dashboard shows. Never includes the key."""
    provider = config.LLM_PROVIDER
    spec = PROVIDERS.get(provider)
    return {"provider": provider, "enabled": enabled(),
            "model": config.LLM_MODEL or (spec[2] if spec else None),
            "wire": spec[0] if spec else None}


def _post(url, payload, headers, timeout):
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise LLMError(f"{e.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        # Deliberately NOT retried. A strategic pass is advisory; a slow provider
        # must never eat the seconds a bid at the close is waiting for.
        raise LLMError(str(e)) from e


def complete(system, user, max_tokens=None, timeout=None, temperature=0.2):
    """One completion. Returns the assistant's text."""
    provider = (config.LLM_PROVIDER or "none").strip().lower()
    if not enabled():
        raise LLMError("LLM is disabled (set LLM_PROVIDER and LLM_API_KEY)")
    spec = PROVIDERS.get(provider)
    if spec is None:
        raise LLMError(f"Unknown LLM_PROVIDER={provider!r}. "
                       f"Known: {', '.join(sorted(PROVIDERS))}")
    wire, default_base, default_model = spec
    base = (config.LLM_BASE_URL or default_base).rstrip("/")
    model = config.LLM_MODEL or default_model
    max_tokens = max_tokens or config.LLM_MAX_TOKENS
    timeout = timeout or config.LLM_TIMEOUT
    key = config.LLM_API_KEY

    if wire == "openai":
        data = _post(f"{base}/chat/completions", {
            "model": model, "max_tokens": max_tokens, "temperature": temperature,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }, {"Authorization": f"Bearer {key}"}, timeout)
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"Unexpected response shape: {str(data)[:300]}") from e

    if wire == "anthropic":
        data = _post(f"{base}/v1/messages", {
            "model": model, "max_tokens": max_tokens, "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }, {"x-api-key": key, "anthropic-version": "2023-06-01"}, timeout)
        try:
            return "".join(b.get("text", "") for b in data["content"]
                           if b.get("type") == "text")
        except (KeyError, TypeError) as e:
            raise LLMError(f"Unexpected response shape: {str(data)[:300]}") from e

    # gemini
    data = _post(
        f"{base}/models/{model}:generateContent?key={key}",
        {"systemInstruction": {"parts": [{"text": system}]},
         "contents": [{"role": "user", "parts": [{"text": user}]}],
         "generationConfig": {"maxOutputTokens": max_tokens,
                              "temperature": temperature}},
        {}, timeout)
    try:
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError(f"Unexpected response shape: {str(data)[:300]}") from e


def complete_json(system, user, **kwargs):
    """A completion parsed as JSON, tolerating the ```json fences models add."""
    text = (complete(system, user, **kwargs) or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise LLMError(f"No JSON object in the reply: {text[:200]}")
    try:
        return json.loads(text[start:end + 1])
    except ValueError as e:
        raise LLMError(f"Malformed JSON from the model: {e}") from e
