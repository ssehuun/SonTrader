"""adapters/notifier_tg.py 테스트 (구현 계획 6단계).

httpx.MockTransport로 텔레그램 API를 흉내낸다(tests/test_client.py와 같은
패턴). 가장 중요하게 보는 것: (1) 킬 스위치 명령이 실제로 DB 상태를
바꾼다, (2) 이 봇은 매매를 지시할 수 없다 — 인라인 버튼도, 콜백 처리도
없다.
"""

import json
from datetime import datetime

import httpx
import pytest

from sontrader.adapters.notifier_tg import TelegramError, TelegramNotifier
from sontrader.data import db
from sontrader.engine import killswitch

NOW = datetime(2026, 3, 10, 9, 0)


def make_notifier(db_engine, responder) -> TelegramNotifier:
    def handler(request: httpx.Request) -> httpx.Response:
        return responder(request)

    return TelegramNotifier(
        "test-token", "12345", db_engine, transport=httpx.MockTransport(handler)
    )


def ok_response(result=None) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result if result is not None else {}})


def test_send_message_posts_to_telegram_with_chat_id_and_text(db_engine):
    calls = []

    def responder(request):
        calls.append(request)
        return ok_response()

    notifier = make_notifier(db_engine, responder)
    notifier.send_message("체결됨: 005930")

    assert len(calls) == 1
    assert calls[0].url.path.endswith("/sendMessage")
    body = json.loads(calls[0].content)
    assert body["chat_id"] == "12345"
    assert body["text"] == "체결됨: 005930"


def test_call_raises_telegram_error_when_ok_is_false(db_engine):
    def responder(request):
        return httpx.Response(200, json={"ok": False, "description": "chat not found"})

    notifier = make_notifier(db_engine, responder)

    with pytest.raises(TelegramError, match="chat not found"):
        notifier.send_message("hi")


def test_process_update_kill_command_engages_kill_switch(db_engine):
    db.migrate(db_engine)
    notifier = make_notifier(db_engine, lambda request: ok_response())

    notifier.process_update({"message": {"text": "/kill"}}, now=NOW)

    assert killswitch.is_engaged(db_engine) is True


def test_process_update_resume_command_disengages_kill_switch(db_engine):
    db.migrate(db_engine)
    killswitch.engage(db_engine, now=NOW)
    notifier = make_notifier(db_engine, lambda request: ok_response())

    notifier.process_update({"message": {"text": "/resume"}}, now=NOW)

    assert killswitch.is_engaged(db_engine) is False


def test_process_update_status_command_reports_kill_switch_state(db_engine):
    db.migrate(db_engine)
    killswitch.engage(db_engine, now=NOW)
    calls = []

    def responder(request):
        calls.append(request)
        return ok_response()

    notifier = make_notifier(db_engine, responder)
    notifier.process_update({"message": {"text": "/status"}}, now=NOW)

    body = json.loads(calls[0].content)
    assert "킬 스위치: 작동 중" in body["text"]


def test_process_update_ignores_unrecognized_command(db_engine):
    db.migrate(db_engine)
    calls = []

    def responder(request):
        calls.append(request)
        return ok_response()

    notifier = make_notifier(db_engine, responder)
    notifier.process_update({"message": {"text": "/unknown"}}, now=NOW)

    assert calls == []


def test_process_update_ignores_callback_queries(db_engine):
    """승인 버튼을 없앴으므로 콜백을 처리하지 않는다. 예전 메시지의 버튼을
    눌러도 매매 상태가 바뀌어서는 안 된다."""
    db.migrate(db_engine)
    calls = []

    def responder(request):
        calls.append(request)
        return ok_response()

    notifier = make_notifier(db_engine, responder)
    notifier.process_update({"callback_query": {"id": "q1", "data": "approve:some-id"}}, now=NOW)

    assert calls == []
    assert killswitch.is_engaged(db_engine) is False


def test_get_updates_returns_result_list(db_engine):
    updates = [{"update_id": 1, "message": {"text": "/status"}}]
    notifier = make_notifier(db_engine, lambda request: ok_response(updates))

    result = notifier.get_updates()

    assert result == updates
