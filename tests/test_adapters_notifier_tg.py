"""adapters/notifier_tg.py 테스트 (구현 계획 6단계).

httpx.MockTransport로 텔레그램 API를 흉내낸다(tests/test_client.py와 같은
패턴). 가장 중요하게 보는 것: (1) 콜백을 받으면 승인 큐 상태가 실제로
바뀐다, (2) 이미 처리됐거나 만료된 제안에 대한 콜백은 조용히 무시되지
않고 사용자에게 에러로 보인다, (3) 킬 스위치 명령이 실제로 DB 상태를
바꾼다.
"""

import json
from datetime import datetime, timedelta

import httpx
import pytest

from sontrader.adapters.notifier_tg import TelegramError, TelegramNotifier
from sontrader.core.types import ExitRule, TargetItem, Urgency
from sontrader.data import db
from sontrader.engine import approval, killswitch

NOW = datetime(2026, 3, 10, 9, 0)


def make_notifier(db_engine, responder) -> TelegramNotifier:
    def handler(request: httpx.Request) -> httpx.Response:
        return responder(request)

    return TelegramNotifier(
        "test-token", "12345", db_engine, transport=httpx.MockTransport(handler)
    )


def ok_response(result=None) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result if result is not None else {}})


def entry_item(*, symbol="005930", event_id="evt-1", weight=0.20) -> TargetItem:
    return TargetItem(
        symbol=symbol,
        weight=weight,
        urgency=Urgency.NEXT_OPEN,
        exit_rule=ExitRule(),
        event_id=event_id,
    )


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


def test_send_approval_request_includes_inline_keyboard_with_proposal_id(db_engine):
    db.migrate(db_engine)
    proposal = approval.propose(db_engine, entry_item(), now=NOW)
    calls = []

    def responder(request):
        calls.append(request)
        return ok_response()

    notifier = make_notifier(db_engine, responder)
    notifier.send_approval_request(proposal)

    body = json.loads(calls[0].content)
    buttons = body["reply_markup"]["inline_keyboard"][0]
    assert buttons[0]["callback_data"] == f"approve:{proposal.proposal_id}"
    assert buttons[1]["callback_data"] == f"reject:{proposal.proposal_id}"


def test_call_raises_telegram_error_when_ok_is_false(db_engine):
    notifier = make_notifier(
        db_engine,
        lambda request: httpx.Response(200, json={"ok": False, "description": "chat not found"}),
    )

    with pytest.raises(TelegramError, match="chat not found"):
        notifier.send_message("hi")


def test_process_update_approves_proposal_on_callback(db_engine):
    db.migrate(db_engine)
    proposal = approval.propose(db_engine, entry_item(), now=NOW)
    notifier = make_notifier(db_engine, lambda request: ok_response())

    update = {
        "callback_query": {
            "id": "cb-1",
            "data": f"approve:{proposal.proposal_id}",
        }
    }
    notifier.process_update(update, now=NOW)

    [decided] = approval.pull_approved(db_engine)
    assert decided.proposal_id == proposal.proposal_id


def test_process_update_still_confirms_when_answer_callback_query_fails(db_engine):
    """텔레그램 콜백 유효시간이 지나 answerCallbackQuery가 400을 반환해도
    (실전 테스트 중 실제로 재현됨), 이미 반영된 결정의 확인 메시지는 여전히
    나가야 한다."""
    db.migrate(db_engine)
    proposal = approval.propose(db_engine, entry_item(), now=NOW)
    calls = []

    def responder(request):
        calls.append(request)
        if request.url.path.endswith("/answerCallbackQuery"):
            return httpx.Response(400, json={"ok": False, "description": "query is too old"})
        return ok_response()

    notifier = make_notifier(db_engine, responder)
    update = {"callback_query": {"id": "cb-1", "data": f"approve:{proposal.proposal_id}"}}

    notifier.process_update(update, now=NOW)  # 예외 없이 끝나야 한다

    [decided] = approval.pull_approved(db_engine)
    assert decided.proposal_id == proposal.proposal_id
    send_message_calls = [c for c in calls if c.url.path.endswith("/sendMessage")]
    assert len(send_message_calls) == 1


def test_process_update_rejects_proposal_on_callback(db_engine):
    db.migrate(db_engine)
    proposal = approval.propose(db_engine, entry_item(), now=NOW)
    notifier = make_notifier(db_engine, lambda request: ok_response())

    update = {"callback_query": {"id": "cb-1", "data": f"reject:{proposal.proposal_id}"}}
    notifier.process_update(update, now=NOW)

    assert approval.list_pending(db_engine) == []
    assert approval.pull_approved(db_engine) == []


def test_process_update_answers_error_for_unknown_proposal(db_engine):
    db.migrate(db_engine)
    calls = []

    def responder(request):
        calls.append(request)
        return ok_response()

    notifier = make_notifier(db_engine, responder)
    update = {"callback_query": {"id": "cb-1", "data": "approve:does-not-exist"}}

    notifier.process_update(update, now=NOW)

    assert len(calls) == 1
    assert calls[0].url.path.endswith("/answerCallbackQuery")


def test_process_update_answers_error_for_already_decided_proposal(db_engine):
    db.migrate(db_engine)
    proposal = approval.propose(db_engine, entry_item(), now=NOW)
    approval.decide(db_engine, proposal.proposal_id, approve=True, now=NOW)
    calls = []

    def responder(request):
        calls.append(request)
        return ok_response()

    notifier = make_notifier(db_engine, responder)
    update = {"callback_query": {"id": "cb-1", "data": f"approve:{proposal.proposal_id}"}}

    notifier.process_update(update, now=NOW)

    # 다시 결정하지 않았으므로 여전히 approved 하나만 남아 있다 (consumed 아님).
    assert len(calls) == 1
    assert calls[0].url.path.endswith("/answerCallbackQuery")
    [still_approved] = approval.pull_approved(db_engine)
    assert still_approved.proposal_id == proposal.proposal_id


def test_process_update_expired_proposal_is_not_approved(db_engine):
    db.migrate(db_engine)
    proposal = approval.propose(db_engine, entry_item(), now=NOW, ttl=timedelta(hours=1))
    notifier = make_notifier(db_engine, lambda request: ok_response())

    update = {"callback_query": {"id": "cb-1", "data": f"approve:{proposal.proposal_id}"}}
    notifier.process_update(update, now=NOW + timedelta(hours=2))

    assert approval.pull_approved(db_engine) == []
    assert approval.list_pending(db_engine) == []


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


def test_process_update_status_command_reports_state(db_engine):
    db.migrate(db_engine)
    approval.propose(db_engine, entry_item(), now=NOW)
    calls = []

    def responder(request):
        calls.append(request)
        return ok_response()

    notifier = make_notifier(db_engine, responder)
    notifier.process_update({"message": {"text": "/status"}}, now=NOW)

    body = json.loads(calls[0].content)
    assert "대기 중 승인: 1건" in body["text"]
    assert "005930" in body["text"]


def test_process_update_ignores_unrecognized_command(db_engine):
    db.migrate(db_engine)
    calls = []

    def responder(request):
        calls.append(request)
        return ok_response()

    notifier = make_notifier(db_engine, responder)
    notifier.process_update({"message": {"text": "/unknown"}}, now=NOW)

    assert calls == []


def test_get_updates_returns_result_list(db_engine):
    updates = [{"update_id": 1, "message": {"text": "/status"}}]
    notifier = make_notifier(db_engine, lambda request: ok_response(updates))

    result = notifier.get_updates()

    assert result == updates


def _capture(db_engine):
    """전송된 페이로드를 모으는 notifier."""
    sent = []

    def responder(request):
        sent.append(json.loads(request.content))
        return ok_response()

    return sent, make_notifier(db_engine, responder)


def test_approval_request_includes_the_symbol_name(db_engine):
    """코드만으로는 어느 종목인지 즉시 알 수 없어 승인 판단이 느려진다."""
    db.migrate(db_engine)
    with db_engine.begin() as conn:
        conn.execute(
            db.symbol_master.insert().values(
                symbol="005930",
                name="삼성전자",
                market="KOSPI",
                updated_at=datetime(2026, 8, 20, 8, 0),
            )
        )
    sent, notifier = _capture(db_engine)

    notifier.send_approval_request(
        approval.propose(db_engine, entry_item(symbol="005930"), now=NOW)
    )

    assert "005930 삼성전자" in sent[0]["text"]


def test_missing_name_falls_back_to_the_code(db_engine):
    """이름 조회가 실패해도 알림은 나가야 한다.

    이 알림은 사람이 승인해야 주문이 나가는 경로다 — 부가 정보 때문에
    승인 요청 자체를 잃으면 매매가 통째로 멈춘다.
    """
    db.migrate(db_engine)  # symbol_master가 비어 있다
    sent, notifier = _capture(db_engine)

    notifier.send_approval_request(
        approval.propose(db_engine, entry_item(symbol="999999"), now=NOW)
    )

    assert "999999" in sent[0]["text"]
