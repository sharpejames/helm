import logging
from typing import Iterator

logger = logging.getLogger(__name__)


class LLMClient:
    """Remote LLM client (Claude via Kiro proxy, or Anthropic direct)."""

    def __init__(self, config: dict):
        cfg = config['llm']
        self.model = cfg.get('model', 'claude-sonnet-4.5')
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

    def complete(self, system: str, messages: list, max_tokens: int = None, timeout: float = None) -> str:
        tokens = max_tokens or self.max_tokens
        if self._mode == 'openai':
            msgs = [{"role": "system", "content": system}] + messages
            client = self._client.with_options(timeout=timeout) if timeout else self._client
            response = client.chat.completions.create(
                model=self.model,
                max_tokens=tokens,
                messages=msgs
            )
            return response.choices[0].message.content
        else:
            response = self._client.messages.create(
                model=self.model,
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
    Uses qwen2.5:3b (non-reasoning) for fast JSON output without thinking overhead."""

    def __init__(self, config: dict):
        cfg = config.get('local_llm', {})
        self.model = cfg.get('model', 'qwen2.5:3b')
        self.max_tokens = cfg.get('max_tokens', 2048)
        self.base_url = cfg.get('base_url', 'http://localhost:11434').rstrip('/')
        self._timeout = cfg.get('timeout', 30)
        import requests as _req
        self._session = _req.Session()

    def complete(self, system: str, messages: list, max_tokens: int = None, timeout: float = None) -> str:
        tokens = max_tokens or self.max_tokens
        t = timeout or self._timeout
        msgs = [{"role": "system", "content": system}] + messages
        payload = {
            "model": self.model,
            "messages": msgs,
            "stream": False,
            "options": {"num_predict": tokens, "temperature": 0.3},
        }
        r = self._session.post(
            f"{self.base_url}/api/chat",
            json=payload,
            timeout=t,
        )
        r.raise_for_status()
        data = r.json()
        content = data.get("message", {}).get("content", "")
        # Strip any <think>...</think> blocks (safety for reasoning models)
        import re
        content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
        if not content:
            raise ValueError(f"Empty response from local LLM: {data}")
        return content


_llm: LLMClient = None
_local_llm: LocalLLMClient = None


def init_llm(config: dict):
    global _llm, _local_llm
    _llm = LLMClient(config)
    if config.get('local_llm', {}).get('enabled', False):
        try:
            _local_llm = LocalLLMClient(config)
            logger.info(f"Local LLM initialized: {_local_llm.model} at {_local_llm.base_url}")
        except Exception as e:
            logger.warning(f"Failed to init local LLM, hybrid mode disabled: {e}")
            _local_llm = None


def get_llm() -> LLMClient:
    return _llm


def get_local_llm() -> LocalLLMClient | None:
    return _local_llm
