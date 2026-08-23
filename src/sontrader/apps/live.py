"""장중 실행 진입점 (구현 계획에 번호 없음). 지금까지 만든 부품
(`broker_kis`, `reconcile`, `killswitch`, `notifier_tg`,
`live_ws`, `live_context`, `loop`)을 한 프로세스로 조립한다.

## 최소 구현

기동 시 `reconcile()` 1회, 이후 텔레그램 폴링 → 사이클 실행을 반복한다.
분봉 웹소켓은 설정돼 있으면 계속 `live_bars`에 쌓이지만, exit_rules는
아직 일봉만 쓴다(`engine/live_context.py`의 결정 사항 참고 — 분봉 기준
ExitRule 파라미터가 검증되지 않았다).

장 운영시간 캘린더는 국내휴장일조회(`data/calendar.py`, CTCA0903R)를
쓴다 — 모의투자는 미지원이라 `settings.paper`면 이 확인 자체를
건너뛴다(장외에도 새 일봉이 없어 자연히 신규 주문은 안 나간다). 실전
계좌에서 휴장일로 확인되면 그날은 매매 사이클(reconcile·주문)을
건너뛰고 텔레그램으로 하루에 한 번만 알린다 — 텔레그램 폴링은 휴장일
에도 계속한다(킬 스위치·상태 조회 명령은 살아 있어야 한다).

포지션 불일치(01문서 §6.5)는 기동 시뿐 아니라 매 사이클 확인한다 —
장중에 계좌 밖에서 수동 거래가 발생하는 등, 부팅 이후에도 같은 위험이
생길 수 있다.
"""

from __future__ import annotations

import signal
import sys
import threading
from datetime import date

import httpx
import sqlalchemy as sa

from sontrader.adapters.broker_kis import KisBroker
from sontrader.adapters.clock import RealClock
from sontrader.adapters.live_ws import LiveTickStream
from sontrader.adapters.notifier_tg import TelegramNotifier
from sontrader.auth import ApprovalKeyManager
from sontrader.client import KisClient
from sontrader.config import (
    load_anthropic_api_key,
    load_database_url,
    load_entry_trigger,
    load_settings,
    load_telegram_bot_token,
    load_telegram_chat_id,
)
from sontrader.core.strategy import EntryTrigger, StrategyConfig
from sontrader.data import calendar, cycle_log, db, live_bars
from sontrader.engine import killswitch
from sontrader.engine import reconcile as reconcile_mod
from sontrader.engine.live_context import JudgeFn, build_context
from sontrader.engine.loop import CycleConfig, Deps, run_cycle
from sontrader.engine.reconcile import ReconcileReport

CYCLE_INTERVAL = 60.0  # 초 — 텔레그램 폴링·이벤트 확인 주기
WS_URL_REAL = "ws://ops.koreainvestment.com:21000"
WS_URL_PAPER = "ws://ops.koreainvestment.com:31000"


def main() -> None:
    settings = load_settings()
    engine = db.get_engine(load_database_url())
    client = KisClient(settings)
    broker = KisBroker(client, engine)
    notifier = _build_notifier(engine)
    cycle_config, judge = _build_entry_config(engine)
    stop = _install_signal_handlers()

    tick_stream: LiveTickStream | None = None
    try:
        report = reconcile_mod.reconcile(engine, broker)
        if report.halt:
            _halt(notifier, report)
            return

        tick_stream = _start_tick_stream(engine, client, settings)
        if notifier is not None:
            notifier.send_message("SonTrader 기동 완료")

        offset: int | None = None
        notified_holiday: date | None = None
        trading_open = False  # 장중/장외 전이 로그를 한 번만 남기기 위한 상태
        while not stop.is_set():
            if notifier is not None:
                offset = _poll_telegram(notifier, offset)

            now = RealClock().now()
            if not settings.paper:
                calendar.refresh_if_needed(engine, client, today=now.date())
                if calendar.is_open(engine, now.date()) is False:
                    if notifier is not None and notified_holiday != now.date():
                        notifier.send_message(f"{now:%Y-%m-%d} 휴장일 — 매매를 쉽니다.")
                        notified_holiday = now.date()
                    stop.wait(CYCLE_INTERVAL)
                    continue

            # 장 운영시간 밖에서는 매매 사이클을 건너뛴다. reconcile 하나가
            # 잔고조회+미체결조회 2회를 쓰므로, 60초마다 돌리면 하루 약 2,880회를
            # 아무 소득 없이 소비한다(유량 한도는 모의 초당 2건). 그 시간에 낸
            # 주문은 KIS가 어차피 거부하고 orders 테이블에 쓰레기만 쌓인다.
            #
            # 텔레그램 폴링은 위에서 이미 끝냈다 — 휴장일 처리와 같은 이유로
            # 킬 스위치·상태 조회는 24시간 살아 있어야 한다.
            if not calendar.is_market_hours(now):
                if trading_open:  # 장중 → 장외 전이에만 남긴다
                    print(f"[{now:%H:%M}] 장 마감 — 매매 사이클 중단", flush=True)
                    trading_open = False
                stop.wait(CYCLE_INTERVAL)
                continue
            if not trading_open:
                print(f"[{now:%H:%M}] 장 시작 — 매매 사이클 시작", flush=True)
                trading_open = True

            report = reconcile_mod.reconcile(engine, broker)
            if report.halt:
                # 중단도 기록한다 — 로그에 구멍만 남으면 "죽었다"와 "멈췄다"를
                # 구분할 수 없다.
                cycle_log.record(
                    engine,
                    ts=now,
                    watchlist_n=0,
                    positions_n=len(report.positions),
                    cash=0,
                    equity=0,
                    halted=True,
                )
                _halt(notifier, report)
                return

            watchlist = _load_watchlist(engine, now.date())
            ctx = build_context(
                engine,
                now=now,
                positions=report.positions,
                cash=broker.cash(),
                watchlist=watchlist,
                judge=judge or (lambda event: None),
            )
            result = run_cycle(ctx, Deps(broker=broker, engine=engine), cycle_config)
            # 사이클마다 무조건 남긴다. "변화가 있을 때만" 조건을 걸면 정작
            # 필요한 "아무 일도 없었다"는 사실이 사라지고, 로그의 구멍이
            # 다운타임인지 무거래인지 구분할 수 없게 된다.
            cycle_log.record(
                engine,
                ts=now,
                watchlist_n=len(watchlist),
                positions_n=len(ctx.positions),
                cash=ctx.cash,
                equity=ctx.equity,
                killswitch_engaged=killswitch.is_engaged(engine),
                orders_n=len(result.orders),
                rejections=result.rejections,
            )

            stop.wait(CYCLE_INTERVAL)
    finally:
        if tick_stream is not None:
            tick_stream.stop()
        if notifier is not None:
            notifier.send_message("SonTrader 종료")
        client.close()


def _build_notifier(engine: sa.engine.Engine) -> TelegramNotifier | None:
    token = load_telegram_bot_token()
    chat_id = load_telegram_chat_id()
    if not token or not chat_id:
        return None
    return TelegramNotifier(token, chat_id, engine)


def _build_entry_config(engine: sa.engine.Engine) -> tuple[CycleConfig, JudgeFn | None]:
    """진입 트리거를 확정하고, 그 트리거가 요구할 때만 judge를 만든다.

    **judge를 만드는 조건이 곧 LLM을 호출하는 조건이다.** `build_context()`는
    받은 judge를 트리거와 무관하게 모든 공시에 적용하므로, 워치리스트
    모드에서 judge를 넘기면 쓰지도 않을 판단에 매 사이클 API 비용이 나간다.
    그래서 트리거를 먼저 읽고 EVENT일 때만 만든다.

    어느 쪽인지 기동 로그에 반드시 남긴다 — 전략이 조용히 바뀌는 것이
    실전에서 가장 위험하다.
    """
    trigger = load_entry_trigger()
    if trigger != "event":
        print("진입 트리거: 워치리스트 순위 — LLM 호출 없음", flush=True)
        return CycleConfig(strategy=StrategyConfig(entry_trigger=EntryTrigger.WATCHLIST_RANK)), None

    judge = _build_judge(engine)
    if judge is None:
        raise RuntimeError(
            "SONTRADER_ENTRY_TRIGGER=event 인데 ANTHROPIC_API_KEY가 없습니다 "
            "— 이 조합은 신규 진입이 영원히 0건이라 조용히 청산만 돌게 됩니다."
        )
    print("진입 트리거: 공시 이벤트 + LLM 판단 (공시마다 API 호출)", flush=True)
    return CycleConfig(strategy=StrategyConfig(entry_trigger=EntryTrigger.EVENT)), judge


def _build_judge(engine: sa.engine.Engine) -> JudgeFn | None:
    """Anthropic만 지원한다 — 여러 제공자 선택 UI는 `cli.py`의 백테스트
    커맨드에만 있고, 상시 가동 데몬에는 최소 구성만 둔다."""
    api_key = load_anthropic_api_key()
    if not api_key:
        return None
    from sontrader.llm.anthropic_backend import AnthropicBackend
    from sontrader.llm.judge import CachingJudge

    return CachingJudge(engine, AnthropicBackend(api_key)).judge


def _start_tick_stream(
    engine: sa.engine.Engine, client: KisClient, settings
) -> LiveTickStream | None:
    """워치리스트가 비어 있으면(유니버스 빌더 미실행 등) 시작하지 않는다."""
    symbols = _load_watchlist(engine, RealClock().now().date())
    if not symbols:
        return None
    approval_http = httpx.Client(base_url=settings.base_url)
    approval_key = ApprovalKeyManager(settings, approval_http).get_key()
    ws_url = WS_URL_PAPER if settings.paper else WS_URL_REAL
    stream = LiveTickStream(ws_url, approval_key, symbols, lambda bar: live_bars.store(engine, bar))
    stream.start()
    return stream


def _load_watchlist(engine: sa.engine.Engine, day: date) -> tuple[str, ...]:
    """`day` **이하** 가장 최근 스냅샷을 쓴다.

    `data/universe.py`는 "장 마감 후 실행 전제"라 오늘 워치리스트는 어제
    종가로 계산돼 `date=어제`로 저장된다 — 오늘 장중에는 `date=오늘`인
    행이 아직 없다(오늘 장이 끝나야 생긴다). `date == day`로 정확히
    일치하는 행만 찾으면 실전 매매 중에는 항상 빈 워치리스트가 되어
    신규 진입이 조용히 0건으로 고정된다.
    """
    columns = db.watchlist_snapshots.c
    with engine.connect() as conn:
        latest = conn.execute(
            sa.select(sa.func.max(columns.date)).where(columns.date <= day)
        ).scalar_one()
        if latest is None:
            return ()
        rows = conn.execute(
            sa.select(columns.symbol).where(columns.date == latest).order_by(columns.rank)
        )
        return tuple(row.symbol for row in rows)


def _poll_telegram(notifier: TelegramNotifier, offset: int | None) -> int | None:
    for update in notifier.get_updates(offset=offset, timeout=0):
        notifier.process_update(update, now=RealClock().now())
        offset = update["update_id"] + 1
    return offset


def _install_signal_handlers() -> threading.Event:
    stop = threading.Event()

    def handler(signum: int, frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
    return stop


def _halt(notifier: TelegramNotifier | None, report: ReconcileReport) -> None:
    detail = ", ".join(f"{m.symbol}({m.reason})" for m in report.mismatches)
    message = f"포지션 불일치 발견 — 매매를 중단합니다: {detail}"
    print(message, file=sys.stderr)
    if notifier is not None:
        notifier.send_message(message)


if __name__ == "__main__":
    main()
