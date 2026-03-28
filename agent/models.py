"""
agent/models.py — Tiered LLM model router for Helm.

Three tiers:
  - LOCAL:  Qwen2.5-3B via Ollama (0ms, plan execution, simple JSON)
  - FAST:   Claude Haiku via Kiro (~1-2s, routine decisions, game moves)
  - SMART:  Claude Opus via Kiro (~6-10s, complex planning, error recovery)

The executor picks the tier based on task complexity.
"""

import logging
import re
from typing import Iterator

logger = logging.getLogger(__name__)

# ── Tier constants ──────────────────────────────────────────────────────────
TIER_LOCAL = "local"   # Ollama qwen2.5:3b — fast, dumb
TIER_FAST = "fast"     # Haiku — quick, decent reasoning
TIER_SMART = "smart"   # Opus — slow, best reasoning


class LLMClient:
    """Remote LLM client (Claude via Kiro proxy). Supports model override per call."""

    def __init__(self, config: dict):
        cfg = config['llm']
        self.model = cfg.get('model', 'claude-opus-4.6')
        self.max_tokens = cfg.get('max_tokens', 4096)
        provider = cfg.get('provider', 'kiro')

        if provider in ('kiro', 'openai-compatible'):
            from openai import OpenAI
            base_url = cfg.get('base_url', 'http://localhost:8000').rstrip('/')
            if not base_url.endswith('/v1'):
                base_url += '/v1'
            self._client = OpenAI(
                base_url=base_url,
                api_key=cfg.get('api_key', 'no-key'),
                timeout=300.0,
            )
            self._mode = 'openai'
        elif provider == 'anthropic':
            import anthropic
            self._client = anthropic.Anthropic(api_key=cfg.get('api_key', ''))
            self._mode = 'anthropic'
        else:
            raise ValueError(f"Unknown provider: {provider}")

    def complete(self, system: str, messages: list, max_tokens: int = None,
                 timeout: float = None, model: str = None) -> str:
        """Complete a chat. Optional model override for tier routing."""
        tokens = max_tokens or self.max_tokens
        use_model = model or self.model
        if self._mode == 'openai':
            msgs = [{"role": "system", "content": system}] + messages
            client = self._client.with_options(timeout=timeout) if timeout else self._client
            response = client.chat.completions.create(
                model=use_model,
                max_tokens=tokens,
                messages=msgs
            )
            return response.choices[0].message.content
        else:
            response = self._client.messages.create(
                model=use_model,
                max_tokens=tokens,
                system=system,
                messages=messages
            )
            return response.content[0].text

    def stream(self, system: str, messages: list) -> Iterator[str]:
        if self._mode == 'openai':
            msgs = [{"role": "system", "content": system}] + messages
            stream = self._client.chat.completions.create(
                model=self.model,
                max_tokens=self.max_tokens,
                messages=msgs,
                stream=True
            )
            for chunk in stream:
                delta = chunk.choices[0].delta.content
                if delta:
                    yield delta
        else:
            with self._client.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=messages
            ) as s:
                for text in s.text_stream:
                    yield text


class LocalLLMClient:
    """Local LLM client via Ollama's native API.
    Supports both text-only and vision (multimodal) queries."""

    def __init__(self, config: dict):
        cfg = config.get('local_llm', {})
        self.model = cfg.get('model', 'qwen3.5:4b')
        self.max_tokens = cfg.get('max_tokens', 2048)
        self.base_url = cfg.get('base_url', 'http://localhost:11434').rstrip('/')
        self._timeout = cfg.get('timeout', 30)
        import requests as _req
        self._session = _req.Session()

    def complete(self, system: str, messages: list, max_tokens: int = None,
                 timeout: float = None) -> str:
        tokens = max_tokens or self.max_tokens
        t = timeout or self._timeout
        msgs = [{"role": "system", "content": system}] + messages
        payload = {
            "model": self.model,
            "messages": msgs,
            "stream": False,
            "think": False,  # Disable thinking for speed
            "options": {"num_predict": tokens, "temperature": 0.3},
        }
        r = self._session.post(f"{self.base_url}/api/chat", json=payload, timeout=t)
        r.raise_for_status()
        data = r.json()
        content = data.get("message", {}).get("content", "")
        content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
        if not content:
            raise ValueError(f"Empty response from local LLM: {data}")
        return content

    def ask_with_image(self, question: str, image_b64: str,
                        max_tokens: int = 512, timeout: float = 30) -> str:
        """Ask a question about an image. Uses Ollama's multimodal support."""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": question, "images": [image_b64]}
            ],
            "stream": False,
            "think": False,  # Disable thinking for speed
            "options": {"num_predict": max_tokens, "temperature": 0.2},
        }
        r = self._session.post(f"{self.base_url}/api/chat", json=payload,
                                timeout=timeout)
        r.raise_for_status()
        data = r.json()
        content = data.get("message", {}).get("content", "")
        content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
        return content or "no response"


class ModelRouter:
    """Routes LLM calls to the right tier based on task complexity.

    Usage:
        router = get_router()
        # Smart call (planning, complex reasoning)
        result = router.complete(system, messages, tier=TIER_SMART)
        # Fast call (routine game moves, simple decisions)
        result = router.complete(system, messages, tier=TIER_FAST)
        # Local call (plan execution, JSON formatting)
        result = router.complete(system, messages, tier=TIER_LOCAL)
    """

    def __init__(self, config: dict):
        self._remote = LLMClient(config)
        self._local: LocalLLMClient | None = None

        # Model names for each tier
        llm_cfg = config.get('llm', {})
        tiers_cfg = config.get('model_tiers', {})
        self.smart_model = tiers_cfg.get('smart', llm_cfg.get('model', 'claude-opus-4.6'))
        self.fast_model = tiers_cfg.get('fast', 'claude-haiku-4.5')

        # Init local
        if config.get('local_llm', {}).get('enabled', False):
            try:
                self._local = LocalLLMClient(config)
                logger.info(f"Local LLM: {self._local.model} at {self._local.base_url}")
            except Exception as e:
                logger.warning(f"Local LLM init failed: {e}")

        logger.info(f"Model tiers: smart={self.smart_model}, fast={self.fast_model}, "
                     f"local={'yes' if self._local else 'no'}")

    @property
    def has_local(self) -> bool:
        return self._local is not None

    def complete(self, system: str, messages: list, tier: str = TIER_SMART,
                 max_tokens: int = None, timeout: float = None) -> str:
        """Route a completion to the appropriate model tier."""
        if tier == TIER_LOCAL and self._local:
            return self._local.complete(system, messages, max_tokens=max_tokens,
                                         timeout=timeout)
        elif tier == TIER_FAST:
            t = timeout or 30.0
            return self._remote.complete(system, messages, max_tokens=max_tokens,
                                          timeout=t, model=self.fast_model)
        else:  # TIER_SMART or fallback
            t = timeout or 90.0
            return self._remote.complete(system, messages, max_tokens=max_tokens,
                                          timeout=t, model=self.smart_model)


# ── Global singletons ──────────────────────────────────────────────────────

_llm: LLMClient = None
_local_llm: LocalLLMClient = None
_router: ModelRouter = None


def init_llm(config: dict):
    global _llm, _local_llm, _router
    _llm = LLMClient(config)
    _router = ModelRouter(config)
    _local_llm = _router._local


def get_llm() -> LLMClient:
    return _llm


def get_local_llm() -> LocalLLMClient | None:
    return _local_llm


def get_router() -> ModelRouter:
    return _router
