"""Command-line interface for quick manual access to quotes, balance, and orders."""

from __future__ import annotations

import argparse
import sys

from sontrader.client import KisClient, KisError
from sontrader.config import load_dart_api_key, load_database_url, load_settings


def _first_line(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc).split(chr(10), 1)[0]}"


def _now_kst():
    # 저장 시각은 naive KST 통일 (스키마 규약).
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone(timedelta(hours=9))).replace(tzinfo=None)


def _parse_date_arg(date_str: str):
    """--date 값(YYYYMMDD) → date. 형식이 틀리면 ValueError."""
    from datetime import datetime

    return datetime.strptime(date_str, "%Y%m%d").date()


def _open_engine():
    """Load DATABASE_URL and build an engine; print + return None on failure.

    Shared by every DB command so the "never echo the URL — it can embed the
    database password" rule lives in exactly one place.
    """
    from sqlalchemy.exc import ArgumentError

    from sontrader.data.db import get_engine

    database_url = load_database_url()
    if not database_url:
        print("error: DATABASE_URL is not set. See env.example.", file=sys.stderr)
        return None
    try:
        return get_engine(database_url)
    except ArgumentError:
        print("error: DATABASE_URL is not a valid SQLAlchemy URL.", file=sys.stderr)
        return None


def _run_migrate() -> int:
    # KIS-only commands must not pay for (or depend on) SQLAlchemy, so the
    # DB stack is imported only when a DB command actually runs.
    from sqlalchemy.exc import SQLAlchemyError

    from sontrader.data.db import migrate

    engine = _open_engine()
    if engine is None:
        return 2
    try:
        actions = migrate(engine)
    except SQLAlchemyError as exc:
        print(f"error: migration failed: {_first_line(exc)}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    for action in actions:
        print(action)
    if not actions:
        print("schema up to date — nothing to do")
    return 0


def _run_collect_dart(date_str: str | None, interval: int | None) -> int:
    # DB/DART-only command: no KIS credentials, lazy heavy imports (see _run_migrate).
    import time as time_module

    import httpx
    from sqlalchemy.exc import SQLAlchemyError

    from sontrader.data.dart import FATAL_STATUSES, DartClient, DartError
    from sontrader.data.dart import ingest as ingest_disclosures
    from sontrader.data.db import migrate

    api_key = load_dart_api_key()
    if not api_key:
        print("error: DART_API_KEY is not set. See env.example.", file=sys.stderr)
        return 2
    fixed_day = None
    if date_str:
        try:
            fixed_day = _parse_date_arg(date_str)
        except ValueError:
            print(f"error: --date must be YYYYMMDD, got {date_str!r}", file=sys.stderr)
            return 2
    if interval is not None and interval < 1:
        print("error: --interval must be >= 1 (seconds).", file=sys.stderr)
        return 2

    now_kst = _now_kst

    engine = _open_engine()
    if engine is None:
        return 2

    seen: set[str] = set()  # 이번 세션에서 이미 적재한 접수번호 — 폴링 중복 insert 방지

    def collect_once(client: DartClient) -> int:
        day = fixed_day or now_kst().date()
        try:
            fresh = [d for d in client.list_disclosures(day) if d.rcept_no not in seen]
            new_count = ingest_disclosures(engine, fresh, ingested_at=now_kst())
            seen.update(d.rcept_no for d in fresh)
            print(f"{day}: 후보 {len(fresh)}건, 신규 적재 {new_count}건")
            return 0
        except DartError as exc:
            print(f"error: DART collection failed: {exc}", file=sys.stderr)
            # 미등록 키 등은 재시도가 무의미하므로 설정 오류(2)로 구분한다.
            return 2 if exc.status in FATAL_STATUSES else 1
        except httpx.HTTPError as exc:
            print(f"error: DART request failed: {exc}", file=sys.stderr)
            return 1
        except SQLAlchemyError as exc:
            print(f"error: DB write failed: {_first_line(exc)}", file=sys.stderr)
            return 1

    try:
        try:
            # 스키마를 먼저 최신으로 보장한다 (멱등이라 이미 최신이면 no-op).
            for action in migrate(engine):
                print(action)
        except SQLAlchemyError as exc:
            print(f"error: migration failed: {_first_line(exc)}", file=sys.stderr)
            return 1
        with DartClient(api_key) as client:
            while True:
                code = collect_once(client)
                if interval is None or code == 2:
                    return code
                time_module.sleep(interval)
    except KeyboardInterrupt:
        return 0
    finally:
        engine.dispose()


def _run_collect_master() -> int:
    import httpx
    from sqlalchemy.exc import SQLAlchemyError

    from sontrader.data.db import migrate
    from sontrader.data.master import MASTER_URLS, fetch_master, upsert_master

    engine = _open_engine()
    if engine is None:
        return 2
    try:
        for action in migrate(engine):
            print(action)
        now = _now_kst()
        for market in MASTER_URLS:
            rows = fetch_master(market)
            count, removed = upsert_master(engine, rows, updated_at=now)
            note = f", 상장폐지 정리 {removed}종목" if removed else ""
            print(f"{market}: {count}종목 갱신{note}")
        return 0
    except httpx.HTTPError as exc:
        print(f"error: master download failed: {exc}", file=sys.stderr)
        return 1
    except SQLAlchemyError as exc:
        print(f"error: DB write failed: {_first_line(exc)}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()


def _empty_universe_hint(engine) -> str:
    """수집 대상이 0종목일 때, 원인에 맞는 안내를 고른다.

    마스터가 아예 없는 것과 구조적 필터가 전부 걸러낸 것은 대응이 다르다.
    후자는 실제로 발생한다 — listing_date 컬럼을 추가한 마이그레이션 직후
    collect-master를 다시 돌리기 전에는 전 행이 NULL이라 fail-closed로 전량
    제외된다. 이때 "테이블이 비었다"고 안내하면 원인을 못 찾는다.
    """
    import sqlalchemy as sa

    from sontrader.data.db import symbol_master

    with engine.connect() as conn:
        total = conn.execute(sa.select(sa.func.count()).select_from(symbol_master)).scalar_one()
    if total == 0:
        return "error: symbol_master is empty — run `sontrader collect-master` first."
    return (
        f"error: symbol_master에 {total}종목이 있지만 수집 대상이 0종목입니다 "
        "— 구조적 필터(core.filters.is_collectable)가 전부 제외했습니다. "
        "listing_date가 비어 있을 수 있으니 `sontrader collect-master`를 다시 실행하세요."
    )


def _run_collect_prices(limit: int | None, pace: float | None, lookback_days: int) -> int:
    from sqlalchemy.exc import SQLAlchemyError

    from sontrader.data.db import migrate
    from sontrader.data.master import load_collectable_symbols
    from sontrader.data.prices import collect_daily_all

    try:
        settings = load_settings()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    engine = _open_engine()
    if engine is None:
        return 2
    try:
        for action in migrate(engine):
            print(action)
        today = _now_kst().date()
        symbols = load_collectable_symbols(engine, today=today)
        if not symbols:
            # "테이블이 비었다"와 "필터가 전부 걸렀다"는 대응이 다르다.
            # 후자는 마이그레이션으로 listing_date를 추가한 직후 collect-master를
            # 아직 안 돌린 상태에서 실제로 발생한다(전 행 NULL → fail-closed).
            print(_empty_universe_hint(engine), file=sys.stderr)
            return 2
        if limit is not None:
            symbols = symbols[:limit]
        # 유량 한도: 모의 초당 2건 / 실전 초당 20건 — 기본 간격을 여유 있게 잡는다.
        pace_seconds = pace if pace is not None else (0.5 if settings.paper else 0.06)

        def on_progress(index: int, total: int) -> None:
            if index % 100 == 0 or index == total:
                print(f"  {index}/{total}", flush=True)

        with KisClient(settings) as client:
            results, failures = collect_daily_all(
                engine,
                client,
                symbols,
                today=today,
                lookback_days=lookback_days,
                pace_seconds=pace_seconds,
                on_progress=on_progress,
            )
        full_count = sum(1 for r in results if r.full)
        print(f"수집 완료: {len(results)}종목 (전체수집 {full_count}, 실패 {len(failures)})")
        for symbol, exc in failures[:5]:
            print(f"  실패 {symbol}: {_first_line(exc)}", file=sys.stderr)
        return 1 if failures else 0
    except SQLAlchemyError as exc:
        print(f"error: DB access failed: {_first_line(exc)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("중단됨 — 지금까지 수집분은 저장되어 있고, 재실행하면 이어서 수집합니다.")
        return 0
    finally:
        engine.dispose()


def _run_build_universe(
    date_str: str | None, min_trade_value: int, lookback: int, skip: int
) -> int:
    from sqlalchemy.exc import SQLAlchemyError

    from sontrader.data.db import migrate
    from sontrader.data.universe import UniverseError, build_snapshot

    as_of = None
    if date_str:
        try:
            as_of = _parse_date_arg(date_str)
        except ValueError:
            print(f"error: --date must be YYYYMMDD, got {date_str!r}", file=sys.stderr)
            return 2
    if lookback < 2 or skip < 0 or skip >= lookback:
        print(
            f"error: require 0 <= --skip < --lookback (got skip={skip}, lookback={lookback}).",
            file=sys.stderr,
        )
        return 2
    engine = _open_engine()
    if engine is None:
        return 2
    try:
        for action in migrate(engine):
            print(action)
        result = build_snapshot(
            engine,
            as_of=as_of or _now_kst().date(),
            lookback=lookback,
            skip=skip,
            min_avg_trade_value=min_trade_value,
        )
        if result.as_of != result.requested:
            gap = (result.requested - result.as_of).days
            note = " — 수집이 밀렸는지 확인 필요" if gap > 3 else ""
            print(f"주의: 최신 일봉 기준으로 스냅샷을 {result.as_of}에 기록 ({gap}일 이전){note}")
        print(
            f"{result.as_of}: 후보 {result.candidates}종목 → 점수 산출 {result.scored}종목"
            f" → 워치리스트 {len(result.entries)}종목"
        )
        for entry in result.entries[:10]:
            print(f"  {entry.rank:>3}. {entry.symbol}  {entry.score:+.2%}")
        return 0
    except UniverseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except SQLAlchemyError as exc:
        print(f"error: DB access failed: {_first_line(exc)}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()


def _build_llm_backend(provider: str, model: str | None, base_url: str | None):
    """--llm-provider에 맞는 LLMBackend를 구성한다. 실패 시 (None, 종료코드)."""
    from sontrader.config import load_anthropic_api_key, load_openai_api_key

    if provider == "anthropic":
        from sontrader.llm.anthropic_backend import AnthropicBackend

        api_key = load_anthropic_api_key()
        if not api_key:
            print("error: ANTHROPIC_API_KEY is not set. See env.example.", file=sys.stderr)
            return None, 2
        kwargs = {"model": model} if model else {}
        return AnthropicBackend(api_key, **kwargs), 0

    from sontrader.llm.openai_backend import OpenAICompatibleBackend

    api_key = load_openai_api_key()
    if not api_key:
        print("error: OPENAI_API_KEY is not set. See env.example.", file=sys.stderr)
        return None, 2
    if not model:
        print("error: --llm-model is required with --llm-provider openai", file=sys.stderr)
        return None, 2
    kwargs = {"base_url": base_url} if base_url else {}
    return OpenAICompatibleBackend(api_key, model=model, **kwargs), 0


def _run_backtest(
    start_str: str,
    end_str: str,
    initial_cash: int,
    use_llm: bool,
    llm_provider: str,
    llm_model: str | None,
    llm_base_url: str | None,
) -> int:
    from sqlalchemy.exc import SQLAlchemyError

    from sontrader.apps.backtest import BacktestError, run_backtest
    from sontrader.apps.report import build_report
    from sontrader.data.db import migrate

    try:
        start = _parse_date_arg(start_str)
        end = _parse_date_arg(end_str)
    except ValueError:
        print("error: --start/--end must be YYYYMMDD", file=sys.stderr)
        return 2
    if start > end:
        print("error: --start must be <= --end", file=sys.stderr)
        return 2

    backend = None
    if use_llm:
        backend, err = _build_llm_backend(llm_provider, llm_model, llm_base_url)
        if backend is None:
            return err

    engine = _open_engine()
    if engine is None:
        return 2
    try:
        for action in migrate(engine):
            print(action)
        judge = None
        if backend is not None:
            from sontrader.llm.judge import CachingJudge

            judge = CachingJudge(engine, backend).judge
        else:
            print("주의: --llm 미지정 — 이번 실행은 신규 진입 없이 청산 로직만 검증합니다.")
        result = run_backtest(engine, start=start, end=end, initial_cash=initial_cash, judge=judge)
        final_equity = result.equity_curve[-1][1] if result.equity_curve else initial_cash
        print(f"{start} ~ {end}: 사이클 {len(result.equity_curve)}일, 체결 {len(result.fills)}건")
        print(
            f"최종 현금 {result.final_cash:,}원, 보유 {len(result.final_positions)}종목,"
            f" 최종 평가자산 {final_equity:,}원"
        )
        if result.rejections:
            print(f"거부 {len(result.rejections)}건 (슬롯/중복이벤트/쿨다운 등)")

        report = build_report(result, initial_cash=initial_cash)
        _print_report(report)
        return 0
    except BacktestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except SQLAlchemyError as exc:
        print(f"error: DB access failed: {_first_line(exc)}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()


def _pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value:+.2%}"


def _num(value: float | None, suffix: str = "") -> str:
    return "N/A" if value is None else f"{value:.2f}{suffix}"


def _print_report(report) -> None:
    from sontrader.apps.report import MIN_TRADE_SAMPLE

    print(f"CAGR {_pct(report.cagr)}  샤프 {_num(report.sharpe)}  MDD {report.mdd:.2%}")
    print(
        f"승률 {_pct(report.win_rate)}  손익비 {_num(report.profit_factor)}"
        f"  평균보유 {_num(report.avg_holding_days, '일')}"
    )
    print(f"거래 {report.trade_count}건  총거래비용비중 {report.cost_ratio:.2%}")
    if report.sample_warning:
        print(f"주의: 거래 표본이 {MIN_TRADE_SAMPLE}건 미만 — 지표 신뢰 구간 밖 (01문서 §5.1).")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sontrader", description="Korean stock trading via the KIS open API"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    quote = sub.add_parser("quote", help="현재가 조회")
    quote.add_argument("code", help="6-digit ticker, e.g. 005930")

    sub.add_parser("balance", help="계좌 잔고 조회")

    sub.add_parser("migrate", help="DB 스키마 생성/확장 (매매 상태 테이블 + 수정주가 컬럼)")

    collect = sub.add_parser("collect-dart", help="DART 공시 수집 → events 적재 (멱등)")
    collect.add_argument("--date", help="YYYYMMDD (기본: 오늘)")
    collect.add_argument(
        "--interval", type=int, default=None, help="초 단위 폴링 루프; 생략하면 1회 실행"
    )

    sub.add_parser("collect-master", help="KOSPI/KOSDAQ 종목 마스터 수집 → symbol_master")

    prices = sub.add_parser("collect-prices", help="일봉 수집 (수정주가, 증분+자가치유)")
    prices.add_argument("--limit", type=int, default=None, help="상위 N종목만 (테스트용)")
    prices.add_argument(
        "--pace", type=float, default=None, help="API 호출 간격 초 (기본: 모의 0.5)"
    )
    prices.add_argument(
        "--lookback-days", type=int, default=420, help="최초 수집 시 소급 일수 (달력일)"
    )

    universe = sub.add_parser(
        "build-universe", help="모멘텀 워치리스트 산출 + 일별 스냅샷 저장 (히스테리시스 50/70)"
    )
    universe.add_argument("--date", help="기준일 YYYYMMDD (기본: 오늘)")
    universe.add_argument(
        "--min-trade-value",
        type=int,
        default=1_000_000_000,
        help="최근 20거래일 평균 거래대금 하한 (KRW, 기본 10억)",
    )
    universe.add_argument("--lookback", type=int, default=252, help="모멘텀 룩백 (거래일)")
    universe.add_argument("--skip", type=int, default=21, help="모멘텀 스킵 (거래일)")

    backtest = sub.add_parser("backtest", help="백테스트 실행")
    backtest.add_argument("--start", required=True, help="시작일 YYYYMMDD")
    backtest.add_argument("--end", required=True, help="종료일 YYYYMMDD")
    backtest.add_argument(
        "--initial-cash", type=int, default=10_000_000, help="초기 자본 KRW (기본 1,000만원)"
    )
    backtest.add_argument(
        "--llm",
        action="store_true",
        help="LLM으로 진입 판단; 생략하면 신규 진입 없이 청산 로직만 검증",
    )
    backtest.add_argument(
        "--llm-provider",
        choices=["anthropic", "openai"],
        default="anthropic",
        help="LLM 제공자 (기본 anthropic; openai는 Azure OpenAI·Ollama 등 호환 서버도 포함)",
    )
    backtest.add_argument(
        "--llm-model",
        default=None,
        help="모델 ID (anthropic 생략 시 claude-opus-5; openai는 필수)",
    )
    backtest.add_argument(
        "--llm-base-url",
        default=None,
        help="--llm-provider openai용 API 베이스 URL (Azure/로컬 서버 지정 시)",
    )

    for side, korean in (("buy", "매수"), ("sell", "매도")):
        order = sub.add_parser(side, help=f"{korean} 주문")
        order.add_argument("code", help="6-digit ticker")
        order.add_argument("quantity", type=int)
        order.add_argument(
            "--price", type=int, default=None, help="limit price in KRW; omit for a market order"
        )

    args = parser.parse_args(argv)

    if args.command == "migrate":
        return _run_migrate()
    if args.command == "collect-dart":
        return _run_collect_dart(args.date, args.interval)
    if args.command == "collect-master":
        return _run_collect_master()
    if args.command == "collect-prices":
        return _run_collect_prices(args.limit, args.pace, args.lookback_days)
    if args.command == "build-universe":
        return _run_build_universe(args.date, args.min_trade_value, args.lookback, args.skip)
    if args.command == "backtest":
        return _run_backtest(
            args.start,
            args.end,
            args.initial_cash,
            args.llm,
            args.llm_provider,
            args.llm_model,
            args.llm_base_url,
        )

    try:
        settings = load_settings()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    mode = "모의투자" if settings.paper else "실전투자"

    with KisClient(settings) as client:
        try:
            if args.command == "quote":
                q = client.get_quote(args.code)
                print(
                    f"{args.code}  현재가 {int(q['stck_prpr']):,}원"
                    f"  전일대비 {q['prdy_vrss']} ({q['prdy_ctrt']}%)"
                )
            elif args.command == "balance":
                b = client.get_balance()
                print(f"[{mode}] 예수금 {int(b['summary'].get('dnca_tot_amt', 0)):,}원")
                for h in b["holdings"]:
                    print(
                        f"  {h['pdno']} {h['prdt_name']}: {int(h['hldg_qty']):,}주"
                        f" @ {float(h['pchs_avg_pric']):,.0f}원"
                        f" (평가손익률 {h['evlu_pfls_rt']}%)"
                    )
            else:
                result = client.order(args.command, args.code, args.quantity, price=args.price)
                print(f"[{mode}] 주문 접수: 주문번호 {result.get('ODNO')}")
        except KisError as exc:
            print(f"KIS error: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
