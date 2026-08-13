"""Claude API 백엔드 (구현 계획 6단계). `LLMBackend` 구현체 중 하나.

`output_config.format`(JSON 스키마)으로 응답 형식을 강제한다 — Anthropic
API가 구조화된 출력을 직접 지원하므로, 자유 텍스트를 파싱해 JSON을
기대하는 실패 경로(따옴표 깨짐, 여분의 설명 문장 등)가 원천적으로 없다.
"""

from __future__ import annotations

import httpx
from anthropic import Anthropic

from sontrader.llm.backend import BackendError

DEFAULT_MODEL = "claude-opus-5"


class AnthropicBackend:
    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model_id = model
        http_client = httpx.Client(transport=transport) if transport is not None else None
        self._client = Anthropic(api_key=api_key, http_client=http_client)

    def complete(self, *, system: str, user: str, schema: dict) -> str:
        response = self._client.messages.create(
            model=self.model_id,
            max_tokens=1024,
            system=system,
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": user}],
        )
        if response.stop_reason == "refusal":
            raise BackendError(f"{self.model_id} refused to respond")

        content = next((block for block in response.content if block.type == "text"), None)
        if content is None:
            raise BackendError(f"no text content in {self.model_id} response")
        return content.text
