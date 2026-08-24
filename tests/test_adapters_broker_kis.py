"""KisBroker 테스트 (구현 계획 7단계).

httpx.MockTransport로 KIS를 흉내내고(tests/test_client.py와 같은 패턴),
db_engine으로 주문 영속화를 검증한다. 가장 중요하게 보는 것: (1) 같은
idempotency_key는 두 번째 제출에서 KIS를 다시 부르지 않는다, (2) 타임아웃은
접수 불명(UNKNOWN)으로 남고 조용히 다른 상태로 둔갑하지 않는다.
"""

import json
from datetime import datetime

import httpx
import pytest

from sontrader.adapters.broker_kis import KisBroker
from sontrader.client import KisClient
from sontrader.core.types import Order as CoreOrder
from sontrader.core.types import OrderStatus, OrderType, Side, Urgency
from sontrader.data import db, orders
from tests.conftest import TOKEN_RESPONSE

NOW = datetime(2026, 3, 10, 9, 30)


def make_client(settings, responder) -> KisClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/tokenP":
            return httpx.Response(200, json=TOKEN_RESPONSE)
        return responder(request)

    return KisClient(settings, transport=httpx.MockTransport(handler))


def make_broker(settings, db_engine, responder) -> KisBroker:
    return KisBroker(make_client(settings, responder), db_engine)


def make_order(symbol: str = "005930", *, side="buy", qty: int = 10, event_id=None):
    return CoreOrder(
        idempotency_key=f"{symbol}:{side}:{NOW.isoformat()}",
        symbol=symbol,
        side=Side.BUY if side == "buy" else Side.SELL,
        qty=qty,
        order_type=OrderType.MARKET,
        urgency=Urgency.NEXT_OPEN,
        ts=NOW,
        event_id=event_id,
    )


def order_cash_response(odno: str = "0000117057") -> httpx.Response:
    return httpx.Response(200, json={"rt_cd": "0", "output": {"ODNO": odno}})


def rejection_response() -> httpx.Response:
    return httpx.Response(
        200, json={"rt_cd": "1", "msg_cd": "APBK0656", "msg1": "주문가능금액을 초과했습니다."}
    )


def balance_response(holdings: list[dict], *, cash: int = 5_000_000) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "rt_cd": "0",
            "output1": holdings,
            "output2": [{"dnca_tot_amt": str(cash)}],
        },
    )


def execution_row(
    *,
    odno: str = "ODNO1",
    ord_qty: int = 10,
    tot_ccld_qty: int = 0,
    rjct_qty: int = 0,
    cncl_yn: str = "",
    avg_prvs: str = "0",
    ord_dt: str = "20260310",
    ord_tmd: str = "093015",
) -> dict:
    return {
        "odno": odno,
        "ord_qty": str(ord_qty),
        "tot_ccld_qty": str(tot_ccld_qty),
        "rjct_qty": str(rjct_qty),
        "cncl_yn": cncl_yn,
        "avg_prvs": avg_prvs,
        "ord_dt": ord_dt,
        "ord_tmd": ord_tmd,
    }


def daily_ccld_response(rows: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"rt_cd": "0", "output1": rows, "output2": {}})


def seed_order(db_engine, order, *, order_id: str, status, broker_order_no: str | None = None):
    orders.insert(
        db_engine,
        order,
        order_id=order_id,
        status=status,
        created_at=NOW,
        broker_order_no=broker_order_no,
    )


# --- 멱등성 -----------------------------------------------------------------------


def test_new_order_is_recorded_unknown_then_updated_to_submitted(db_engine, settings):
    db.migrate(db_engine)
    calls = []

    def responder(request):
        calls.append(request)
        return order_cash_response("0000117057")

    broker = make_broker(settings, db_engine, responder)
    order = make_order()

    [result] = broker.submit([order], now=NOW)

    assert result.status.value == "submitted"
    assert result.broker_order_no == "0000117057"
    assert len(calls) == 1
    record = orders.find_by_idempotency_key(db_engine, order.idempotency_key)
    assert record.status.value == "submitted"
    assert record.broker_order_no == "0000117057"


def test_repeated_idempotency_key_does_not_resubmit(db_engine, settings):
    db.migrate(db_engine)
    calls = []

    def responder(request):
        calls.append(request)
        return order_cash_response()

    broker = make_broker(settings, db_engine, responder)
    order = make_order()

    first = broker.submit([order], now=NOW)
    second = broker.submit([order], now=NOW)

    assert len(calls) == 1  # 두 번째는 KIS를 다시 부르지 않는다
    assert first[0].broker_order_no == second[0].broker_order_no


def test_multiple_orders_are_each_recorded(db_engine, settings):
    db.migrate(db_engine)

    broker = make_broker(settings, db_engine, lambda request: order_cash_response())
    a = make_order("005930")
    b = make_order("000660")

    results = broker.submit([a, b], now=NOW)

    assert len(results) == 2
    assert orders.find_by_idempotency_key(db_engine, a.idempotency_key) is not None
    assert orders.find_by_idempotency_key(db_engine, b.idempotency_key) is not None


# --- 실패 경로 ----------------------------------------------------------------------


def test_kis_rejection_is_recorded_as_rejected(db_engine, settings):
    db.migrate(db_engine)
    broker = make_broker(settings, db_engine, lambda request: rejection_response())
    order = make_order()

    [result] = broker.submit([order], now=NOW)

    assert result.status.value == "rejected"
    record = orders.find_by_idempotency_key(db_engine, order.idempotency_key)
    assert record.status.value == "rejected"


def test_timeout_is_recorded_as_unknown(db_engine, settings):
    db.migrate(db_engine)

    def responder(request):
        raise httpx.ReadTimeout("timed out", request=request)

    broker = make_broker(settings, db_engine, responder)
    order = make_order()

    [result] = broker.submit([order], now=NOW)

    assert result.status.value == "unknown"
    record = orders.find_by_idempotency_key(db_engine, order.idempotency_key)
    assert record.status.value == "unknown"
    assert record.broker_order_no is None


def test_unresolved_order_after_timeout_shows_up_for_restart_recovery(db_engine, settings):
    db.migrate(db_engine)

    def responder(request):
        raise httpx.ReadTimeout("timed out", request=request)

    broker = make_broker(settings, db_engine, responder)
    broker.submit([make_order()], now=NOW)

    unresolved = orders.list_unresolved(db_engine)

    assert len(unresolved) == 1
    assert unresolved[0].status.value == "unknown"


# --- 접수 불명 해소 (resolve_unknown) --------------------------------------------


def test_resolve_confirms_full_fill(db_engine, settings):
    db.migrate(db_engine)
    order = make_order(qty=10)
    seed_order(
        db_engine, order, order_id="ord-1", status=OrderStatus.SUBMITTED, broker_order_no="O1"
    )
    row = execution_row(odno="O1", ord_qty=10, tot_ccld_qty=10, avg_prvs="71000")
    broker = make_broker(settings, db_engine, lambda request: daily_ccld_response([row]))

    [result] = broker.resolve_unknown()

    assert result.status.value == "filled"
    assert result.fills[0].price == 71000
    assert result.fills[0].qty == 10
    record = orders.find_by_idempotency_key(db_engine, order.idempotency_key)
    assert record.status.value == "filled"


def test_resolve_confirms_partial_fill(db_engine, settings):
    db.migrate(db_engine)
    order = make_order(qty=10)
    seed_order(
        db_engine, order, order_id="ord-1", status=OrderStatus.SUBMITTED, broker_order_no="O1"
    )
    row = execution_row(odno="O1", ord_qty=10, tot_ccld_qty=4, avg_prvs="71000")
    broker = make_broker(settings, db_engine, lambda request: daily_ccld_response([row]))

    [result] = broker.resolve_unknown()

    assert result.status.value == "partial"
    assert result.fills[0].qty == 4


def test_resolve_confirms_rejection(db_engine, settings):
    db.migrate(db_engine)
    order = make_order(qty=10)
    seed_order(db_engine, order, order_id="ord-1", status=OrderStatus.UNKNOWN, broker_order_no="O1")
    row = execution_row(odno="O1", ord_qty=10, tot_ccld_qty=0, rjct_qty=10)
    broker = make_broker(settings, db_engine, lambda request: daily_ccld_response([row]))

    [result] = broker.resolve_unknown()

    assert result.status.value == "rejected"
    assert result.fills == ()


def test_resolve_confirms_cancellation(db_engine, settings):
    db.migrate(db_engine)
    order = make_order(qty=10)
    seed_order(
        db_engine, order, order_id="ord-1", status=OrderStatus.SUBMITTED, broker_order_no="O1"
    )
    row = execution_row(odno="O1", ord_qty=10, tot_ccld_qty=0, cncl_yn="Y")
    broker = make_broker(settings, db_engine, lambda request: daily_ccld_response([row]))

    [result] = broker.resolve_unknown()

    assert result.status.value == "cancelled"


def test_resolve_upgrades_unknown_to_submitted_when_still_pending(db_engine, settings):
    db.migrate(db_engine)
    order = make_order(qty=10)
    seed_order(db_engine, order, order_id="ord-1", status=OrderStatus.UNKNOWN, broker_order_no="O1")
    row = execution_row(odno="O1", ord_qty=10, tot_ccld_qty=0)
    broker = make_broker(settings, db_engine, lambda request: daily_ccld_response([row]))

    [result] = broker.resolve_unknown()

    # 이 행이 존재한다는 것 자체가 KIS가 접수했다는 뜻이다.
    assert result.status.value == "submitted"


def test_resolve_leaves_status_unchanged_when_no_row_found(db_engine, settings):
    db.migrate(db_engine)
    order = make_order(qty=10)
    seed_order(db_engine, order, order_id="ord-1", status=OrderStatus.UNKNOWN, broker_order_no="O1")
    broker = make_broker(settings, db_engine, lambda request: daily_ccld_response([]))

    [result] = broker.resolve_unknown()

    assert result.status.value == "unknown"


def test_resolve_skips_orders_without_a_broker_order_no(db_engine, settings):
    db.migrate(db_engine)
    order = make_order(qty=10)
    seed_order(db_engine, order, order_id="ord-1", status=OrderStatus.UNKNOWN, broker_order_no=None)

    def responder(request):  # pragma: no cover - must never be reached
        raise AssertionError("daily-ccld should not be called without an ODNO")

    broker = make_broker(settings, db_engine, responder)

    assert broker.resolve_unknown() == []


def test_resolve_replaces_fill_snapshot_instead_of_accumulating(db_engine, settings):
    db.migrate(db_engine)
    order = make_order(qty=10)
    seed_order(
        db_engine, order, order_id="ord-1", status=OrderStatus.SUBMITTED, broker_order_no="O1"
    )
    responses = [
        daily_ccld_response(
            [execution_row(odno="O1", ord_qty=10, tot_ccld_qty=4, avg_prvs="70000")]
        ),
        daily_ccld_response(
            [execution_row(odno="O1", ord_qty=10, tot_ccld_qty=10, avg_prvs="71000")]
        ),
    ]
    calls = iter(responses)
    broker = make_broker(settings, db_engine, lambda request: next(calls))

    broker.resolve_unknown()  # 4/10 체결
    broker.resolve_unknown()  # 10/10 체결로 갱신

    fills = orders.load_fills(db_engine, "ord-1")
    assert len(fills) == 1  # 누적되지 않고 최신 스냅샷 하나로 교체됨
    assert fills[0].qty == 10
    assert fills[0].price == 71000


# --- 잔고 ---------------------------------------------------------------------------


def test_positions_maps_kis_balance_holdings(db_engine, settings):
    db.migrate(db_engine)
    holdings = [
        {"pdno": "005930", "hldg_qty": "100", "pchs_avg_pric": "71000.5"},
        {"pdno": "000660", "hldg_qty": "0", "pchs_avg_pric": "0"},  # 청산된 종목 — 제외돼야 함
    ]
    broker = make_broker(settings, db_engine, lambda request: balance_response(holdings))

    positions = broker.positions()

    assert len(positions) == 1
    assert positions[0].symbol == "005930"
    assert positions[0].qty == 100
    assert positions[0].avg_price == pytest.approx(71000.5)


def test_cash_returns_available_amount(db_engine, settings):
    db.migrate(db_engine)
    broker = make_broker(settings, db_engine, lambda request: balance_response([], cash=3_210_000))

    assert broker.cash() == 3_210_000


# --- 매도 ---------------------------------------------------------------------------


def test_sell_order_is_submitted_correctly(db_engine, settings):
    db.migrate(db_engine)
    captured = {}

    def responder(request):
        captured["tr_id"] = request.headers["tr_id"]
        captured["body"] = json.loads(request.content)
        return order_cash_response()

    broker = make_broker(settings, db_engine, responder)
    order = make_order("005930", side="sell", qty=5)

    broker.submit([order], now=NOW)

    assert captured["tr_id"] == "VTTC0011U"  # paper sell
    assert captured["body"]["ORD_QTY"] == "5"


# --- 일시 오류 재시도 / 거절 사유 보존 ------------------------------------------


def _order_response(odno: str = "0000000123") -> httpx.Response:
    return httpx.Response(200, json={"rt_cd": "0", "msg_cd": "APBK0013", "output": {"ODNO": odno}})


def _kis_error(code: str, msg: str) -> httpx.Response:
    return httpx.Response(200, json={"rt_cd": "1", "msg_cd": code, "msg1": msg})


def test_transient_rate_limit_is_retried_and_then_succeeds(settings, db_engine):
    """유량 초과는 접수 전 거절이라 재시도가 안전하다.

    재시도가 없으면 초당 유량에 한 번 걸린 IMMEDIATE 청산이 그 사이클을
    통째로 날린다 — 다음 사이클에 새 멱등 키로 재시도되긴 하지만 60초 지연이다.
    """
    db.migrate(db_engine)
    attempts = []

    def responder(request):
        attempts.append(request)
        if len(attempts) < 3:
            return _kis_error("EGW00201", "초당 거래건수를 초과하였습니다.")
        return _order_response()

    broker = KisBroker(make_client(settings, responder), db_engine, sleep=lambda _s: None)
    (result,) = broker.submit([make_order()], now=NOW)

    assert result.status is OrderStatus.SUBMITTED
    assert result.broker_order_no == "0000000123"
    assert len(attempts) == 3


def test_permanent_rejection_is_not_retried_and_keeps_the_reason(settings, db_engine):
    """잔고 부족은 재시도해도 같은 답이다 — 즉시 거절하되 사유를 남긴다."""
    db.migrate(db_engine)
    attempts = []

    def responder(request):
        attempts.append(request)
        return _kis_error("APBK0656", "주문가능금액이 부족합니다.")

    broker = KisBroker(make_client(settings, responder), db_engine, sleep=lambda _s: None)
    (result,) = broker.submit([make_order()], now=NOW)

    assert result.status is OrderStatus.REJECTED
    assert len(attempts) == 1  # 재시도하지 않는다
    assert "APBK0656" in result.reason
    assert "주문가능금액" in result.reason


def test_exhausted_transient_retries_end_in_rejection_with_reason(settings, db_engine):
    db.migrate(db_engine)
    attempts = []

    def responder(request):
        attempts.append(request)
        return _kis_error("EGW00201", "초당 거래건수를 초과하였습니다.")

    broker = KisBroker(make_client(settings, responder), db_engine, sleep=lambda _s: None)
    (result,) = broker.submit([make_order()], now=NOW)

    assert result.status is OrderStatus.REJECTED
    assert "EGW00201" in result.reason
    assert len(attempts) == 3
    stored = orders.find_by_idempotency_key(db_engine, make_order().idempotency_key)
    assert stored.status is OrderStatus.REJECTED


# --- 로그 (실전 관측의 유일한 창구) ------------------------------------------------


def test_submitted_order_is_logged_with_its_odno(settings, db_engine, caplog):
    """**실제 자금이 움직이기 시작한 지점**이라 로그가 반드시 있어야 한다.
    ODNO를 남겨야 나중에 KIS 앱에서 같은 주문을 찾아 대조할 수 있다.

    회귀 테스트인 이유: 이 로그가 한 번 누락된 채로 전체 테스트가 통과했고,
    실제로 코드를 돌려 눈으로 봤을 때야 빠진 것이 드러났다.
    """
    db.migrate(db_engine)

    def responder(request):
        return httpx.Response(200, json={"rt_cd": "0", "output": {"ODNO": "0000117057"}})

    broker = make_broker(settings, db_engine, responder)
    with caplog.at_level("INFO", logger="sontrader.adapters.broker_kis"):
        broker.submit([make_order()], now=NOW)

    [record] = [r for r in caplog.records if "주문 제출" in r.message]
    assert record.levelname == "INFO"
    assert "005930" in record.message
    assert "0000117057" in record.message  # ODNO


def test_duplicate_submission_is_logged(settings, db_engine, caplog):
    """중복 차단이 조용하면 "주문이 안 나갔다"와 "이미 나갔다"를 구분할 수 없다."""
    db.migrate(db_engine)
    calls = []

    def responder(request):
        calls.append(request)
        return httpx.Response(200, json={"rt_cd": "0", "output": {"ODNO": "0000117057"}})

    broker = make_broker(settings, db_engine, responder)
    order = make_order()
    broker.submit([order], now=NOW)
    with caplog.at_level("INFO", logger="sontrader.adapters.broker_kis"):
        broker.submit([order], now=NOW)  # 같은 멱등 키

    assert len(calls) == 1  # KIS를 다시 부르지 않았다
    assert any("중복 주문 차단" in r.message for r in caplog.records)


def test_rejected_order_is_logged_at_error_with_the_reason(settings, db_engine, caplog):
    """사유가 없으면 "잔고 부족"과 "유량 초과"를 구분할 수 없다."""
    db.migrate(db_engine)

    def responder(request):
        return httpx.Response(
            200, json={"rt_cd": "1", "msg_cd": "40310000", "msg1": "주문가능금액이 부족합니다"}
        )

    broker = make_broker(settings, db_engine, responder)
    with caplog.at_level("INFO", logger="sontrader.adapters.broker_kis"):
        broker.submit([make_order()], now=NOW)

    [record] = [r for r in caplog.records if "주문 거절" in r.message]
    assert record.levelname == "ERROR"
    assert "주문가능금액이 부족합니다" in record.message


def test_unknown_acceptance_is_logged_at_warning(settings, db_engine, caplog):
    """접수 불명은 다음 사이클에 해소되지만, 실제로 접수됐을 수 있어
    조용히 넘기면 안 된다."""
    db.migrate(db_engine)

    def responder(request):
        raise httpx.ReadTimeout("timeout")

    broker = make_broker(settings, db_engine, responder)
    with caplog.at_level("INFO", logger="sontrader.adapters.broker_kis"):
        result = broker.submit([make_order()], now=NOW)

    assert result[0].status is OrderStatus.UNKNOWN
    [record] = [r for r in caplog.records if "접수 불명" in r.message]
    assert record.levelname == "WARNING"


def test_missing_odno_warning_fires_only_once_per_order(settings, db_engine, caplog):
    """`list_unresolved()`는 나이 제한이 없고 이 주문은 상태가 영영 바뀌지
    않는다. 조건 없이 찍으면 하루 약 390회 같은 경고가 반복되어 정작 조치가
    필요한 WARNING을 묻어 버린다."""
    db.migrate(db_engine)
    order = make_order()
    orders.insert(db_engine, order, order_id="oid-1", status=OrderStatus.UNKNOWN, created_at=NOW)

    broker = make_broker(settings, db_engine, lambda request: httpx.Response(200, json={}))
    with caplog.at_level("INFO", logger="sontrader.adapters.broker_kis"):
        broker.resolve_unknown()
        broker.resolve_unknown()
        broker.resolve_unknown()

    warnings = [r for r in caplog.records if "ODNO가 없어" in r.message]
    assert len(warnings) == 1, f"3번 호출했지만 경고는 1회여야 한다 (실제 {len(warnings)}회)"
