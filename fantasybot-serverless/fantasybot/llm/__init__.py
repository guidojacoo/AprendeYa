"""The optional LLM brain.

Optional is the operative word: with LLM_PROVIDER unset the bot is 100%
deterministic and spends nothing. Turning it on adds judgement (which targets to
stretch for, what to remember week to week), never execution.
"""

from .client import LLMError, complete, complete_json, describe, enabled

__all__ = ["complete", "complete_json", "describe", "enabled", "LLMError"]
