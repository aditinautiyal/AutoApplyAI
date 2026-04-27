"""
core/api_router.py
Unified AI API layer. Switches between Claude and OpenAI via config.
Haiku/GPT-3.5 for cheap tasks. Sonnet/GPT-4 for cover letters.
"""

import asyncio
from typing import Optional
from core.settings_store import get_store

# Model tiers
CLAUDE_FAST  = "claude-haiku-4-5-20251001"
CLAUDE_SMART = "claude-sonnet-4-6"
GPT_FAST     = "gpt-3.5-turbo"
GPT_SMART    = "gpt-4-turbo-preview"


class APIRouter:
    def __init__(self):
        self.store = get_store()
        self._anthropic_client = None
        self._openai_client = None

    @property
    def provider(self) -> str:
        """'claude' or 'openai' based on which key is configured."""
        if self.store.get("claude_api_key"):
            return "claude"
        if self.store.get("openai_api_key"):
            return "openai"
        return "none"

    def _get_anthropic(self):
        if not self._anthropic_client:
            import anthropic
            key = self.store.get("claude_api_key")
            if not key:
                raise ValueError("No Claude API key configured.")
            self._anthropic_client = anthropic.Anthropic(api_key=key)
        return self._anthropic_client

    def _get_openai(self):
        if not self._openai_client:
            from openai import OpenAI
            key = self.store.get("openai_api_key")
            if not key:
                raise ValueError("No OpenAI API key configured.")
            self._openai_client = OpenAI(api_key=key)
        return self._openai_client

    def complete(self, prompt: str, system: str = "", smart: bool = False,
                 max_tokens: int = 2000) -> str:
        """
        Synchronous completion.
        smart=True uses better model (Sonnet/GPT-4) — for cover letters only.
        smart=False uses fast/cheap model (Haiku/GPT-3.5) — for everything else.
        """
        if self.provider == "claude":
            return self._claude_complete(prompt, system, smart, max_tokens)
        elif self.provider == "openai":
            return self._openai_complete(prompt, system, smart, max_tokens)
        else:
            raise ValueError(
                "No AI API key configured. Add Claude or OpenAI key in Settings."
            )

    def _claude_complete(self, prompt: str, system: str, smart: bool,
                          max_tokens: int) -> str:
        client = self._get_anthropic()
        model = CLAUDE_SMART if smart else CLAUDE_FAST
        messages = [{"role": "user", "content": prompt}]
        kwargs = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        response = client.messages.create(**kwargs)
        return response.content[0].text

    def _openai_complete(self, prompt: str, system: str, smart: bool,
                          max_tokens: int) -> str:
        client = self._get_openai()
        model = GPT_SMART if smart else GPT_FAST
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens
        )
        return response.choices[0].message.content

    async def complete_async(self, prompt: str, system: str = "",
                              smart: bool = False, max_tokens: int = 2000) -> str:
        """Async wrapper — runs sync completion in thread pool."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: self.complete(prompt, system, smart, max_tokens)
        )

    def test_connection(self) -> tuple[bool, str]:
        """Test API key works. Returns (success, message)."""
        try:
            result = self.complete("Say OK", max_tokens=5)
            return True, f"Connected ({self.provider})"
        except Exception as e:
            return False, str(e)


# Singleton
_router_instance = None

def get_router() -> APIRouter:
    global _router_instance
    if _router_instance is None:
        _router_instance = APIRouter()
    return _router_instance
