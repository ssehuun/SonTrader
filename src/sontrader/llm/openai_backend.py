"""OpenAI Chat Completions 호환 백엔드 (구현 계획 6단계). `LLMBackend` 구현체.

OpenAI 자체뿐 아니라 같은 wire protocol을 쓰는 것(Azure OpenAI, 그리고
Ollama·vLLM·LM Studio 같은 로컬 서버 다수)을 `base_url`만 바꿔서 씀
— 그래서 이름이 "OpenAI"가 아니라 "OpenAICompatible"이다.

## 왜 `response_format: json_object`만 쓰는가

Anthropic 백엔드처럼 구조화된 출력(정확한 JSON 스키마 강제)을 쓰고 싶지만,
그 기능은 서버마다 지원 여부가 갈린다 — 모든 호환 서버가 지원하는 공통
분모는 "유효한 JSON 객체 하나만 반환"(`json_object` 모드)뿐이다. 그래서
실제 스키마는 프롬프트에 설명으로 포함해 모델이 따르도록 유도하고, 진짜
검증(닫힌 집합·범위 확인)은 `llm/judge.py`가 `core.types`의 데이터클래스로
한다(fail-closed) — `LLMBackend` 프로토콜이 애초에 스키마 강제를 보장하지
않는다고 명시한 이유다.
"""

from __future__ import annotations

import json

import httpx

from sontrader.llm.backend import BackendError

DEFAULT_BASE_URL = "https://api.openai.com/v1"


class OpenAICompatibleBackend:
    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model_id = model
        self._api_key = api_key
        self._http = httpx.Client(base_url=base_url, timeout=30.0, transport=transport)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> OpenAICompatibleBackend:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def complete(self, *, system: str, user: str, schema: dict) -> str:
        schema_hint = (
            f"{user}\n\n다음 JSON 스키마를 정확히 따르는 JSON 객체 하나만"
            f" 응답하세요(다른 텍스트 없이):\n{json.dumps(schema, ensure_ascii=False)}"
        )
        response = self._http.post(
            "/chat/completions",
            headers={"authorization": f"Bearer {self._api_key}"},
            json={
                "model": self.model_id,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": schema_hint},
                ],
            },
        )
        response.raise_for_status()
        data = response.json()
        choice = data["choices"][0]
        if choice.get("finish_reason") == "content_filter":
            raise BackendError(f"{self.model_id} refused to respond (content_filter)")

        content = choice["message"]["content"]
        if not content:
            raise BackendError(f"empty response content from {self.model_id}")
        return content
