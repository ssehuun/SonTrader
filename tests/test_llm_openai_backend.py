"""OpenAICompatibleBackend 테스트 (구현 계획 6단계).

httpx.MockTransport로 실제 네트워크 없이 검증한다. OpenAI 자체뿐 아니라
Azure OpenAI·Ollama·vLLM 등 같은 wire protocol을 쓰는 서버 전반을
`base_url`만 바꿔 지원한다는 게 이 클래스의 요점이라, base_url 오버라이드도
확인한다.
"""

import json

import httpx
import pytest

from sontrader.llm.backend import BackendError
from sontrader.llm.openai_backend import OpenAICompatibleBackend


def make_completion_response(content: str | None, *, finish_reason: str = "stop") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-1",
            "choices": [
                {
                    "finish_reason": finish_reason,
                    "message": {"role": "assistant", "content": content},
                }
            ],
        },
    )


def make_backend(responder, **kwargs) -> OpenAICompatibleBackend:
    return OpenAICompatibleBackend(
        "sk-test", model="gpt-test", transport=httpx.MockTransport(responder), **kwargs
    )


def test_model_id_matches_constructor_arg():
    backend = make_backend(lambda request: make_completion_response("{}"))
    assert backend.model_id == "gpt-test"


def test_complete_returns_message_content():
    backend = make_backend(lambda request: make_completion_response('{"진입": false}'))

    text = backend.complete(system="sys", user="user", schema={"type": "object"})

    assert json.loads(text) == {"진입": False}


def test_complete_sends_json_object_mode_and_embeds_schema_in_the_prompt():
    captured = {}

    def responder(request):
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer sk-test"
        captured["body"] = json.loads(request.content)
        return make_completion_response("{}")

    backend = make_backend(responder)
    schema = {"type": "object", "properties": {"진입": {"type": "boolean"}}}

    backend.complete(system="시스템 프롬프트", user="사용자 메시지", schema=schema)

    body = captured["body"]
    assert body["model"] == "gpt-test"
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0] == {"role": "system", "content": "시스템 프롬프트"}
    sent_user = body["messages"][1]["content"]
    assert "사용자 메시지" in sent_user
    assert json.dumps(schema, ensure_ascii=False) in sent_user


def test_content_filter_finish_reason_raises_backend_error():
    backend = make_backend(
        lambda request: make_completion_response(None, finish_reason="content_filter")
    )

    with pytest.raises(BackendError):
        backend.complete(system="sys", user="user", schema={"type": "object"})


def test_empty_content_raises_backend_error():
    backend = make_backend(lambda request: make_completion_response(""))

    with pytest.raises(BackendError):
        backend.complete(system="sys", user="user", schema={"type": "object"})


def test_http_error_status_propagates():
    backend = make_backend(lambda request: httpx.Response(401, json={"error": "bad key"}))

    with pytest.raises(httpx.HTTPStatusError):
        backend.complete(system="sys", user="user", schema={"type": "object"})


def test_custom_base_url_is_honored():
    def responder(request):
        assert str(request.url) == "https://my-server.local/v1/chat/completions"
        return make_completion_response("{}")

    backend = make_backend(responder, base_url="https://my-server.local/v1")
    backend.complete(system="sys", user="user", schema={"type": "object"})
