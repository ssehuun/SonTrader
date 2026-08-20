"""engine/reconcile.py 테스트 (구현 계획 5단계 잔여 작업).

httpx.MockTransport로 KIS를 흉내낸다(tests/test_adapters_broker_kis.py와 같은
패턴). 가장 중요하게 보는 것: (1) 브로커와 DB 양쪽에 있는 종목만 포지션으로
재구성된다, (2) 한쪽에만 있는 종목은 재구성하지 않고 미스매치로 남겨
`halt=True`를 만든다(02문서 §6 "정합성 테스트: 잔고 불일치 주입 → 매매
중단 확인"), (3) 포지션 대조 전에 미체결 주문부터 해소한다.
"""

from datetime import datetime

import httpx

from sontrader.adapters.broker_kis import KisBroker
from sontrader.client import KisClient
from sontrader.core.types import ExitRule, OrderStatus, OrderType, Side, Urgency
from sontrader.core.types import Order as CoreOrder
from sontrader.data import db, orders
from sontrader.data import positions as positions_repo
from sontrader.engine.reconcile import reconcile
from tests.conftest import TOKEN_RESPONSE

NOW = datetime(2026, 3, 10, 9, 30)


def make_broker(settings, db_engine, responder) -> KisBroker:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/tokenP":
            return httpx.Response(200, json=TOKEN_RESPONSE)
        return responder(request)

    client = KisClient(settings, transport=httpx.MockTransport(handler))
    return KisBroker(client, db_engine)


def balance_response(holdings: list[dict], *, cash: int = 5_000_000) -> httpx.Response:
    return httpx.Response(
        200,
        json={"rt_cd": "0", "output1": holdings, "output2": [{"dnca_tot_amt": str(cash)}]},
    )


def holding(symbol: str, qty: int = 10, avg_price: str = "71000") -> dict:
    return {"pdno": symbol, "hldg_qty": str(qty), "pchs_avg_pric": avg_price}


def daily_ccld_response(rows: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"rt_cd": "0", "output1": rows, "output2": {}})


def execution_row(*, odno: str, ord_qty: int = 10, tot_ccld_qty: int = 10) -> dict:
    return {
        "odno": odno,
        "ord_qty": str(ord_qty),
        "tot_ccld_qty": str(tot_ccld_qty),
        "rjct_qty": "0",
        "cncl_yn": "",
        "avg_prvs": "71000",
        "ord_dt": "20260310",
        "ord_tmd": "093015",
    }


def seed_position(
    db_engine, *, symbol: str, qty: int = 10, avg_price: str = "70000", entered_at=NOW
) -> None:
    with db_engine.begin() as conn:
        conn.execute(
            db.positions.insert().values(
                symbol=symbol,
                qty=qty,
                avg_price=avg_price,
                entered_at=entered_at,
                event_id=None,
                exit_rule_json=ExitRule().to_dict(),
            )
        )


def test_reconcile_merges_broker_and_db_state_for_matched_symbol(db_engine, settings):
    db.migrate(db_engine)
    seed_position(db_engine, symbol="005930", avg_price="70000")
    broker = make_broker(
        settings, db_engine, lambda request: balance_response([holding("005930", 10, "71000")])
    )

    report = reconcile(db_engine, broker)

    assert not report.halt
    assert report.mismatches == ()
    [position] = report.positions
    assert position.symbol == "005930"
    assert position.qty == 10
    assert position.avg_price == 71000.0  # 브로커 값이 원본 (설계 6.5절)


def test_reconcile_flags_db_only_position_as_mismatch(db_engine, settings):
    db.migrate(db_engine)
    seed_position(db_engine, symbol="005930", qty=10)
    broker = make_broker(settings, db_engine, lambda request: balance_response([]))

    report = reconcile(db_engine, broker)

    assert report.halt
    assert report.positions == ()
    [mismatch] = report.mismatches
    assert mismatch.symbol == "005930"
    assert mismatch.reason == "db_only"
    assert mismatch.db_qty == 10
    assert mismatch.broker_qty is None


def test_reconcile_flags_broker_only_position_as_mismatch(db_engine, settings):
    db.migrate(db_engine)
    broker = make_broker(
        settings, db_engine, lambda request: balance_response([holding("005930", 5, "71000")])
    )

    report = reconcile(db_engine, broker)

    assert report.halt
    assert report.positions == ()
    [mismatch] = report.mismatches
    assert mismatch.symbol == "005930"
    assert mismatch.reason == "broker_only"
    assert mismatch.broker_qty == 5
    assert mismatch.db_qty is None


def test_reconcile_resolves_unresolved_orders_before_comparing_positions(db_engine, settings):
    db.migrate(db_engine)
    core_order = CoreOrder(
        idempotency_key="005930:buy:2026-03-10T09:30:00",
        symbol="005930",
        side=Side.BUY,
        qty=10,
        order_type=OrderType.MARKET,
        urgency=Urgency.NEXT_OPEN,
        ts=NOW,
    )
    orders.insert(
        db_engine,
        core_order,
        order_id="ord-1",
        status=OrderStatus.UNKNOWN,
        created_at=NOW,
        broker_order_no="O1",
    )

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/uapi/domestic-stock/v1/trading/inquire-daily-ccld":
            return daily_ccld_response([execution_row(odno="O1")])
        return balance_response([])

    broker = make_broker(settings, db_engine, responder)

    report = reconcile(db_engine, broker)

    [resolved] = report.resolved_orders
    assert resolved.status.value == "filled"
    record = orders.find_by_idempotency_key(db_engine, core_order.idempotency_key)
    assert record.status.value == "filled"


def test_reconcile_reports_no_mismatches_when_nothing_held(db_engine, settings):
    db.migrate(db_engine)
    broker = make_broker(settings, db_engine, lambda request: balance_response([]))

    report = reconcile(db_engine, broker)

    assert not report.halt
    assert report.positions == ()
    assert report.mismatches == ()
    assert report.resolved_orders == ()


# --- 체결 → positions 반영 (T1) ------------------------------------------------


def seed_submitted_order(
    db_engine, *, symbol: str, odno: str, side=Side.BUY, qty: int = 10, exit_rule=None
) -> None:
    orders.insert(
        db_engine,
        CoreOrder(
            idempotency_key=f"{symbol}:{side.value}:{NOW.isoformat()}",
            symbol=symbol,
            side=side,
            qty=qty,
            order_type=OrderType.MARKET,
            urgency=Urgency.NEXT_OPEN,
            ts=NOW,
            exit_rule=exit_rule,
        ),
        order_id=f"ord-{odno}",
        status=OrderStatus.SUBMITTED,
        created_at=NOW,
        broker_order_no=odno,
    )


def test_resolved_buy_is_recorded_so_the_next_cycle_does_not_halt(db_engine, settings):
    """체결이 positions에 남지 않으면 브로커에만 있는 종목이 되어 매매가 영구 중단된다.

    2026-08-20 첫 실전 운영이 정확히 이 상태였다 — positions에 쓰는 코드가
    아예 없어서, 체결 한 번이면 다음 사이클부터 halt였다.
    """
    db.migrate(db_engine)
    rule = ExitRule(stop_loss_pct=-0.08, max_hold_days=45)
    seed_submitted_order(db_engine, symbol="005930", odno="0001", exit_rule=rule)

    def responder(request):
        if "inquire-daily-ccld" in request.url.path:
            return daily_ccld_response([execution_row(odno="0001")])
        return balance_response([holding("005930", 10, "71000")])

    report = reconcile(db_engine, make_broker(settings, db_engine, responder))

    assert not report.halt
    [position] = report.positions
    assert position.symbol == "005930"
    # 진입 시점에 확정한 청산 조건이 살아남아야 스톱을 걸 수 있다
    assert position.exit_rule == rule


def test_resolved_sell_removes_the_position(db_engine, settings):
    db.migrate(db_engine)
    seed_position(db_engine, symbol="005930", qty=10)
    seed_submitted_order(db_engine, symbol="005930", odno="0002", side=Side.SELL, qty=10)

    def responder(request):
        if "inquire-daily-ccld" in request.url.path:
            return daily_ccld_response([execution_row(odno="0002")])
        return balance_response([])  # 전량 매도돼 잔고에서 사라졌다

    report = reconcile(db_engine, make_broker(settings, db_engine, responder))

    assert not report.halt
    assert report.positions == ()
    assert positions_repo.load_all(db_engine) == []


def test_partial_sell_keeps_the_position(db_engine, settings):
    """부분 매도는 보유를 유지한다 — 전량 체결에만 청산으로 본다."""
    db.migrate(db_engine)
    seed_position(db_engine, symbol="005930", qty=10)
    seed_submitted_order(db_engine, symbol="005930", odno="0003", side=Side.SELL, qty=10)

    def responder(request):
        if "inquire-daily-ccld" in request.url.path:
            return daily_ccld_response([execution_row(odno="0003", tot_ccld_qty=4)])
        return balance_response([holding("005930", 6, "70000")])

    report = reconcile(db_engine, make_broker(settings, db_engine, responder))

    assert not report.halt
    assert [p.symbol for p in positions_repo.load_all(db_engine)] == ["005930"]


def test_buy_without_exit_rule_falls_back_to_default(db_engine, settings):
    """청산 조건 없는 포지션은 스톱이 영영 발동하지 않는다 — 기본값을 붙인다."""
    db.migrate(db_engine)
    seed_submitted_order(db_engine, symbol="005930", odno="0004", exit_rule=None)

    def responder(request):
        if "inquire-daily-ccld" in request.url.path:
            return daily_ccld_response([execution_row(odno="0004")])
        return balance_response([holding("005930", 10, "71000")])

    report = reconcile(db_engine, make_broker(settings, db_engine, responder))

    [position] = report.positions
    assert position.exit_rule == ExitRule()
