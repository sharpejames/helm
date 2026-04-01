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

import requests

logger = logging.getLogger(__name__)

# ── Tier constants ──────────────────────────────────────────────────────────
TIER_LOCAL = "local"   # Ollama qwen2.5:3b — fast, dumb
TIER_FAST = "fast"     # Haiku — quick, decent reasoning
TIER_SMART = "smart"   # Opus — slow, best reasoning

# Vision tiers (Requirement 15.2, 15.3)
TIER_VISION_FAST = "vision_fast"       # Qwen3.5:0.8B — frame analysis, <1s
TIER_VISION_DETAILED = "vision_detail" # Qwen3.5:4B — detailed scene analysis

# VRAM budget constants (Requirement 15.1, 15.4)
VRAM_BUDGET_GB = 8.0
VRAM_WARNING_GB = 7.5


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

        # Vision model config (Requirement 15.2, 15.3)
        vision_cfg = config.get('vision', {})
        self.vision_fast_model = vision_cfg.get('fast_model', 'qwen3.5:0.8b')
        self.vision_detailed_model = vision_cfg.get('detailed_model', 'qwen3.5:4b')

        # VRAM budget config (Requirement 15.1, 15.4)
        vram_cfg = config.get('vram', {})
        self._vram_budget_gb = vram_cfg.get('budget_gb', VRAM_BUDGET_GB)
        self._vram_warning_gb = vram_cfg.get('warning_gb', VRAM_WARNING_GB)

        # Ollama base URL for vision and VRAM queries
        local_cfg = config.get('local_llm', {})
        self._ollama_url = local_cfg.get('base_url', 'http://localhost:11434').rstrip('/')

        # Init local
        if local_cfg.get('enabled', False):
            try:
                self._local = LocalLLMClient(config)
                logger.info(f"Local LLM: {self._local.model} at {self._local.base_url}")
            except Exception as e:
                logger.warning(f"Local LLM init failed: {e}")

        logger.info(f"Model tiers: smart={self.smart_model}, fast={self.fast_model}, "
                     f"local={'yes' if self._local else 'no'}")
        logger.info(f"Vision models: fast={self.vision_fast_model}, "
                     f"detailed={self.vision_detailed_model}")
        logger.info(f"VRAM budget: {self._vram_budget_gb}GB, "
                     f"warning at {self._vram_warning_gb}GB")

        # Log VRAM usage at startup (Requirement 15.5)
        try:
            vram_info = self.check_vram()
            logger.info(f"Startup VRAM: {vram_info['used_gb']:.2f}GB used, "
                         f"models loaded: {vram_info['models']}, "
                         f"over_budget: {vram_info['over_budget']}")
        except Exception as e:
            logger.warning(f"Could not check VRAM at startup: {e}")

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

    def vision_complete(self, prompt: str, image_b64: str,
                        tier: str = TIER_VISION_FAST) -> str:
        """Route vision request to appropriate Qwen model via Ollama.

        Implements automatic tier escalation: Local vision → Fast → Smart on failure.
        (Requirements 5.5, 5.6, 15.2, 15.3)
        """
        # Select model based on vision tier
        if tier == TIER_VISION_DETAILED:
            model = self.vision_detailed_model
        else:
            model = self.vision_fast_model

        # Attempt local Ollama vision first
        try:
            self.ensure_vram_budget()
            payload = {
                "model": model,
                "messages": [
                    {"role": "user", "content": prompt, "images": [image_b64]}
                ],
                "stream": False,
                "think": False,
                "options": {"num_predict": 512, "temperature": 0.2},
            }
            r = requests.post(f"{self._ollama_url}/api/chat",
                              json=payload, timeout=60)
            r.raise_for_status()
            data = r.json()
            content = data.get("message", {}).get("content", "")
            content = re.sub(r'<think>.*?</think>', '', content,
                             flags=re.DOTALL).strip()
            if content:
                # Log VRAM after model load event (Requirement 15.5)
                try:
                    vram_info = self.check_vram()
                    logger.debug(f"VRAM after vision call ({model}): "
                                 f"{vram_info['used_gb']:.2f}GB")
                except Exception:
                    pass
                return content
            raise ValueError(f"Empty vision response from {model}")
        except Exception as e:
            logger.warning(f"Vision local ({model}) failed: {e}, "
                           f"escalating to Fast tier")

        # Escalate to Fast tier (Requirement 5.5)
        try:
            messages = [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{image_b64}"
                }}
            ]}]
            return self._remote.complete(
                system="You are a vision analysis assistant.",
                messages=messages,
                model=self.fast_model,
                timeout=30.0,
            )
        except Exception as e:
            logger.warning(f"Vision Fast tier failed: {e}, "
                           f"escalating to Smart tier")

        # Escalate to Smart tier (Requirement 5.6)
        messages = [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {
                "url": f"data:image/png;base64,{image_b64}"
            }}
        ]}]
        return self._remote.complete(
            system="You are a vision analysis assistant.",
            messages=messages,
            model=self.smart_model,
            timeout=90.0,
        )

    def check_vram(self) -> dict:
        """Query Ollama /api/ps for loaded models and VRAM usage.

        Returns: {"used_gb": float, "models": list[str], "over_budget": bool}
        (Requirements 15.1, 15.5)
        """
        r = requests.get(f"{self._ollama_url}/api/ps", timeout=5)
        r.raise_for_status()
        data = r.json()

        models = []
        total_vram_bytes = 0
        for entry in data.get("models", []):
            name = entry.get("name", entry.get("model", "unknown"))
            models.append(name)
            # Ollama reports size_vram in bytes
            total_vram_bytes += entry.get("size_vram", 0)

        used_gb = total_vram_bytes / (1024 ** 3)
        over_budget = used_gb > self._vram_warning_gb

        return {
            "used_gb": used_gb,
            "models": models,
            "over_budget": over_budget,
        }

    def ensure_vram_budget(self):
        """If VRAM > 7.5GB, unload the 4B model to free memory.

        Uses Ollama /api/generate with keep_alive=0 to trigger unload.
        (Requirement 15.4)
        """
        try:
            vram_info = self.check_vram()
        except Exception as e:
            logger.warning(f"VRAM check failed, skipping budget enforcement: {e}")
            return

        if not vram_info["over_budget"]:
            return

        logger.warning(f"VRAM over budget: {vram_info['used_gb']:.2f}GB > "
                        f"{self._vram_warning_gb}GB — unloading "
                        f"{self.vision_detailed_model}")

        try:
            payload = {
                "model": self.vision_detailed_model,
                "keep_alive": 0,
            }
            r = requests.post(f"{self._ollama_url}/api/generate",
                              json=payload, timeout=10)
            r.raise_for_status()
            logger.info(f"Unloaded {self.vision_detailed_model} to free VRAM")

            # Log VRAM after unload (Requirement 15.5)
            vram_after = self.check_vram()
            logger.info(f"VRAM after unload: {vram_after['used_gb']:.2f}GB, "
                         f"models: {vram_after['models']}")
        except Exception as e:
            logger.error(f"Failed to unload {self.vision_detailed_model}: {e}")


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
