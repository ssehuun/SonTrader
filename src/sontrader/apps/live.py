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

import logging
import signal
import threading
from collections import Counter
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
from sontrader.logging_setup import configure as configure_logging

# `python -m sontrader.apps.live`로 띄우면 __name__이 "__main__"이 되어
# 로그에 모듈 경로가 안 남는다. 이름을 고정해 다른 모듈과 형식을 맞춘다.
log = logging.getLogger("sontrader.apps.live")

CYCLE_INTERVAL = 60.0  # 초 — 텔레그램 폴링·이벤트 확인 주기
# 장외에는 이 틱 수마다 한 번만 유휴 하트비트를 남긴다(60초 × 30 = 30분).
# 장중처럼 매 틱 찍으면 밤사이 로그가 매매 기록을 덮어 버린다.
IDLE_HEARTBEAT_EVERY = 30
WS_URL_REAL = "ws://ops.koreainvestment.com:21000"
WS_URL_PAPER = "ws://ops.koreainvestment.com:31000"


def main() -> None:
    configure_logging()
    settings = load_settings()
    engine = db.get_engine(load_database_url())
    _sync_schema(engine)
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
        log.info(
            "기동 완료 — %s, 워치리스트 %d종목, 분봉 스트림 %s",
            "모의투자" if settings.paper else "실전",
            len(_load_watchlist(engine, RealClock().now().date())),
            "on" if tick_stream is not None else "off",
        )
        if notifier is not None:
            notifier.send_message("SonTrader 기동 완료")

        offset: int | None = None
        notified_holiday: date | None = None
        trading_open = False  # 장중/장외 전이 로그를 한 번만 남기기 위한 상태
        cycle = 1  # 하트비트 번호 — 로그에서 사이클을 세고 구멍을 찾는 기준
        idle = 0  # 장외 대기 틱 수 — 유휴 하트비트 주기 판정용
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
                    idle = _log_idle(idle, "휴장일")
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
                    log.info("장 마감 — 매매 사이클 중단")
                    trading_open = False
                idle = _log_idle(idle, "장외")
                stop.wait(CYCLE_INTERVAL)
                continue
            if not trading_open:
                log.info("장 시작 — 매매 사이클 시작")
                trading_open = True
                idle = 0

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
                watchlist_ranks=_load_watchlist_ranks(engine, now.date()),
                judge=judge or (lambda event: None),
            )
            result = run_cycle(ctx, Deps(broker=broker, engine=engine), cycle_config)
            engaged = killswitch.is_engaged(engine)
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
                killswitch_engaged=engaged,
                orders_n=len(result.orders),
                rejections=result.rejections,
            )
            _log_heartbeat(cycle, ctx, result, engaged=engaged)
            cycle += 1

            stop.wait(CYCLE_INTERVAL)
    except Exception:
        # 처리하지 못한 오류로 죽는 경로. 이 핸들러가 없으면 파이썬이
        # 트레이스백을 **stderr**로만 내보내는데 로거는 stdout을 쓰므로,
        # 로그 스트림에는 아래 finally의 "종료" 한 줄만 남는다 — 크래시와
        # 정상 종료가 로그상 완전히 같아진다. 상시 가동에서 "조용히 죽으면
        # 인지조차 못 한다"(01문서 §6.4)는 바로 이 상황이다.
        #
        # 로그만 남기고 다시 올린다. 삼키면 종료 코드가 0이 되어 supervisor가
        # 재시작하지 않는다.
        log.exception("비정상 종료 — 처리하지 못한 오류")
        raise
    finally:
        if tick_stream is not None:
            tick_stream.stop()
        log.info("종료 — 정리 완료")
        if notifier is not None:
            notifier.send_message("SonTrader 종료")
        client.close()


def _sync_schema(engine: sa.engine.Engine) -> None:
    """기동 시 DB 스키마를 코드에 맞춘다. 무엇을 적용했는지 반드시 남긴다.

    스키마가 뒤처진 채로 기동하면 **장중에** psycopg2 UndefinedColumn으로
    터진다. 두 번 겪었다:

    - `orders.exit_rule_json` — 첫 `reconcile()`의 미체결 조회가 트레이스백
      으로 죽었다. 스택 어디에도 "마이그레이션 미실행"이 안 적혀 있어 코드
      버그처럼 보였다.
    - `stock_candles_1m.source` — 웹소켓 저장이 실패했고, 재연결 루프가 예외를
      삼켜 **5초마다 분봉을 버리면서 데몬은 계속 살아 있었다.** 더 나쁘다.

    거부하지 않고 적용하는 이유: `migrate()`는 additive-only(컬럼·테이블 추가만,
    삭제·재작성 불가)라 위험이 낮은 반면, 기동을 거부하면 데몬이 죽어 있다.
    "장중 루프가 죽으면 손절이 발동하지 않는다"(01문서 §6.4)가 더 큰 손실이다.
    CLI 수집기들도 이미 같은 방식으로 스스로 `migrate()`를 돌린다.

    적용 **전에** 무엇을 할지 먼저 남긴다 — 마이그레이션이 실패해도 무엇을
    시도했는지가 로그에 남아야 원인을 찾을 수 있다.
    """
    try:
        pending = db.pending_migrations(engine)
        if not pending:
            log.debug("DB 스키마 최신 — 마이그레이션 없음")
            return
        log.warning("DB 스키마가 코드보다 뒤처져 있다 — 적용한다: %s", ", ".join(pending))
        for action in db.migrate(engine):
            log.warning("  %s", action)
    except sa.exc.SQLAlchemyError:
        # 여기서 실패하면 뒤따르는 모든 DB 작업이 깨진다. 매매를 시작하는
        # 것보다 기동을 멈추는 게 낫다 — 주문은 아직 하나도 안 냈다.
        log.exception("DB 스키마 동기화 실패 — 기동을 중단한다")
        raise SystemExit(2) from None


def _log_heartbeat(cycle: int, ctx, result, *, engaged: bool) -> None:
    """사이클마다 한 줄. **살아 있다는 증거가 로그에 있어야 한다.**

    원래는 "사이클마다 INFO를 찍지 않는다"(설계 §6.6.2)였다. 이유는 60초 ×
    420회면 노이즈가 된다는 것이었고, "살아 있었다"는 `cycle_log` 테이블이
    답한다는 전제였다. 실제로 돌려 보니 그 전제가 틀렸다 — 09:00 "장 시작"
    이후 15:30 "장 마감"까지 **54분간 아무것도 안 찍혀서**, 돌고 있는지 죽은
    건지 사람이 구분할 수 없었다. DB를 열어 보지 않으면 알 수 없는 정보는
    "왜 죽었나"에 답하는 이벤트 로그의 몫이 아니다.

    한 줄 약 120바이트 × 420회 = 하루 약 50KB다. 노이즈 우려보다 관측 공백이
    비싸다. 대신 **한 줄로 압축**하고, 거부 사유별 내역처럼 큰 것은 여전히
    `cycle_log`에 맡긴다.
    """
    reasons = Counter(r.reason.value for r in result.rejections)
    parts = [
        f"사이클 {cycle}",
        f"워치 {len(ctx.watchlist)}",
        f"보유 {len(ctx.positions)}",
        f"평가 {ctx.equity:,}원",
        f"주문 {len(result.orders)}",
    ]
    if reasons:
        parts.append("거부 " + "/".join(f"{k} {v}" for k, v in reasons.most_common()))
    if engaged:
        parts.append("킬스위치 작동")
    log.info(" | ".join(parts))


def _log_idle(idle: int, reason: str) -> int:
    """장외·휴장일 대기 중에도 주기적으로 살아있음을 남기고, 다음 틱 수를 반환한다.

    장중 하트비트만으로는 관측 공백이 하루 17시간 넘게 남는다 — "장 마감"
    한 줄 뒤로 다음 "장 시작"까지 아무것도 안 찍히면, **밤사이 죽은 프로세스와
    정상 대기가 로그상 완전히 같다.** 매매를 쉬는 동안에도 프로세스는 텔레그램
    폴링과 킬 스위치를 처리하므로 살아 있어야 하고, 살아 있다는 증거가 필요하다.

    장중(60초)보다 훨씬 드물게 남긴다 — 밤사이 1,000줄이 쌓이면 정작 그날의
    매매 기록을 찾기 어려워진다.
    """
    if idle % IDLE_HEARTBEAT_EVERY == 0:
        log.info("%s 대기 중 — 매매 사이클은 쉬고 텔레그램·킬 스위치만 처리합니다", reason)
    return idle + 1


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
        log.info("진입 트리거: 워치리스트 순위 — LLM 호출 없음")
        return CycleConfig(strategy=StrategyConfig(entry_trigger=EntryTrigger.WATCHLIST_RANK)), None

    judge = _build_judge(engine)
    if judge is None:
        raise RuntimeError(
            "SONTRADER_ENTRY_TRIGGER=event 인데 ANTHROPIC_API_KEY가 없습니다 "
            "— 이 조합은 신규 진입이 영원히 0건이라 조용히 청산만 돌게 됩니다."
        )
    log.info("진입 트리거: 공시 이벤트 + LLM 판단 (공시마다 API 호출)")
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


def _load_watchlist_ranks(engine: sa.engine.Engine, day: date) -> dict[str, int]:
    """종목 → **저장된** rank. `_load_watchlist`와 같은 스냅샷을 본다.

    위치+1로 대체할 수 없다 — 히스테리시스로 순위에 구멍이 뚫려 있다
    (`core.types.Context.watchlist_ranks`).
    """
    columns = db.watchlist_snapshots.c
    with engine.connect() as conn:
        latest = conn.execute(
            sa.select(sa.func.max(columns.date)).where(columns.date <= day)
        ).scalar_one()
        if latest is None:
            return {}
        rows = conn.execute(sa.select(columns.symbol, columns.rank).where(columns.date == latest))
        return {row.symbol: row.rank for row in rows}


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
    log.error(message)
    if notifier is not None:
        notifier.send_message(message)


if __name__ == "__main__":
    main()
