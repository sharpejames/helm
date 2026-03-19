import logging
from typing import Iterator

logger = logging.getLogger(__name__)

class LLMClient:
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
            self._client = OpenAI(base_url=base_url, api_key=cfg.get('api_key', 'no-key'))
            self._mode = 'openai'
        elif provider == 'anthropic':
            import anthropic
            self._client = anthropic.Anthropic(api_key=cfg.get('api_key', ''))
            self._mode = 'anthropic'
        else:
            raise ValueError(f"Unknown provider: {provider}")

    def complete(self, system: str, messages: list) -> str:
        if self._mode == 'openai':
            msgs = [{"role": "system", "content": system}] + messages
            response = self._client.chat.completions.create(
                model=self.model,
                max_tokens=self.max_tokens,
                messages=msgs
            )
            return response.choices[0].message.content
        else:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
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

_llm: LLMClient = None

def init_llm(config: dict):
    global _llm
    _llm = LLMClient(config)

def get_llm() -> LLMClient:
    return _llm
