"""AnthropicBackend 테스트 (구현 계획 6단계).

httpx.MockTransport로 실제 네트워크 없이 검증한다(tests/test_client.py와
같은 패턴). `LLMBackend.complete()` 계약만 본다 — 캐싱·파싱은
test_llm_judge.py가 스텁 백엔드로 이미 검증했다.
"""

import json

import httpx
import pytest

from sontrader.llm.anthropic_backend import DEFAULT_MODEL, AnthropicBackend
from sontrader.llm.backend import BackendError


def make_response(payload: dict | None, *, stop_reason: str = "end_turn") -> httpx.Response:
    content = (
        []
        if payload is None
        else [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]
    )
    return httpx.Response(
        200,
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5",
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10},
        },
    )


def make_backend(responder, **kwargs) -> AnthropicBackend:
    return AnthropicBackend("sk-test", transport=httpx.MockTransport(responder), **kwargs)


def test_model_id_defaults_to_the_documented_model():
    backend = make_backend(lambda request: make_response({}))
    assert backend.model_id == DEFAULT_MODEL


def test_model_id_is_overridable():
    backend = make_backend(lambda request: make_response({}), model="claude-sonnet-5")
    assert backend.model_id == "claude-sonnet-5"


def test_complete_returns_the_text_content():
    payload = {"진입": True, "확신도": 0.5}
    backend = make_backend(lambda request: make_response(payload))

    text = backend.complete(system="sys", user="user", schema={"type": "object"})

    assert json.loads(text) == payload


def test_complete_sends_model_system_and_json_schema():
    captured = {}

    def responder(request):
        captured["body"] = json.loads(request.content)
        return make_response({"ok": True})

    backend = make_backend(responder, model="claude-opus-5")
    schema = {"type": "object", "properties": {"진입": {"type": "boolean"}}}

    backend.complete(system="시스템 프롬프트", user="사용자 메시지", schema=schema)

    body = captured["body"]
    assert body["model"] == "claude-opus-5"
    assert body["system"] == "시스템 프롬프트"
    assert body["messages"] == [{"role": "user", "content": "사용자 메시지"}]
    assert body["output_config"]["format"] == {"type": "json_schema", "schema": schema}


def test_refusal_raises_backend_error():
    backend = make_backend(lambda request: make_response(None, stop_reason="refusal"))

    with pytest.raises(BackendError):
        backend.complete(system="sys", user="user", schema={"type": "object"})


def test_missing_text_block_raises_backend_error():
    # end_turn인데도 텍스트 블록이 없는 비정상 응답 — 조용히 넘어가지 않는다.
    backend = make_backend(lambda request: make_response(None, stop_reason="end_turn"))

    with pytest.raises(BackendError):
        backend.complete(system="sys", user="user", schema={"type": "object"})
