"""장중 실행 진입점 (구현 계획에 번호 없음). 지금까지 만든 부품
(`broker_kis`, `reconcile`, `approval`, `killswitch`, `notifier_tg`,
`live_ws`, `live_context`, `loop`)을 한 프로세스로 조립한다.

## 최소 구현

기동 시 `reconcile()` 1회, 이후 텔레그램 폴링 → 사이클 실행을 반복한다.
분봉 웹소켓은 설정돼 있으면 계속 `live_bars`에 쌓이지만, exit_rules는
아직 일봉만 쓴다(`engine/live_context.py`의 결정 사항 참고 — 분봉 기준
ExitRule 파라미터가 검증되지 않았다).

장 운영시간 캘린더는 없다(01문서 §8 미확정 파라미터) — 사이클을 계속
돌리되, 장외 시간에는 새 일봉이 없어 자연히 신규 주문이 안 나간다.

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
    load_settings,
    load_telegram_bot_token,
    load_telegram_chat_id,
)
from sontrader.data import db, live_bars
from sontrader.engine import reconcile as reconcile_mod
from sontrader.engine.live_context import JudgeFn, build_context
from sontrader.engine.loop import Deps, run_cycle
from sontrader.engine.reconcile import ReconcileReport

CYCLE_INTERVAL = 60.0  # 초 — 텔레그램 폴링·이벤트/승인 확인 주기
WS_URL_REAL = "ws://ops.koreainvestment.com:21000"
WS_URL_PAPER = "ws://ops.koreainvestment.com:31000"


def main() -> None:
    settings = load_settings()
    engine = db.get_engine(load_database_url())
    client = KisClient(settings)
    broker = KisBroker(client, engine)
    notifier = _build_notifier(engine)
    judge = _build_judge(engine)
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
        while not stop.is_set():
            if notifier is not None:
                offset = _poll_telegram(notifier, offset)

            report = reconcile_mod.reconcile(engine, broker)
            if report.halt:
                _halt(notifier, report)
                return

            now = RealClock().now()
            ctx = build_context(
                engine,
                now=now,
                positions=report.positions,
                cash=broker.cash(),
                watchlist=_load_watchlist(engine, now.date()),
                judge=judge or (lambda event: None),
            )
            run_cycle(ctx, Deps(broker=broker, engine=engine, notifier=notifier))

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
    columns = db.watchlist_snapshots.c
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(columns.symbol).where(columns.date == day).order_by(columns.rank)
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
