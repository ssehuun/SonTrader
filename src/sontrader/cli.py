"""Command-line interface for quick manual access to quotes, balance, and orders."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import timedelta

from sontrader.client import KisClient, KisError
from sontrader.config import load_dart_api_key, load_database_url, load_settings
from sontrader.timeutil import now_kst


def _first_line(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc).split(chr(10), 1)[0]}"


def _now_kst():
    # 저장 시각은 naive KST 통일 (스키마 규약) — 정의는 `timeutil` 한 곳.
    return now_kst()


def _display_width(text: str) -> int:
    """터미널에서 차지하는 칸 수. 한글·한자는 두 칸이다.

    글자 수로 폭을 맞추면 종목명이 섞인 열이 어긋난다 — 분봉 수집은 수천 줄을
    쏟아내므로 눈으로 훑을 수 있어야 한다.
    """
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _parse_date_arg(date_str: str):
    """--date 값(YYYYMMDD) → date. 형식이 틀리면 ValueError."""
    from datetime import datetime

    return datetime.strptime(date_str, "%Y%m%d").date()


# API 호출 간격 기본값.
#
# 문서상 한도(실전 초당 20건 / 모의 초당 2건)를 그대로 환산한 값(0.06 / 0.5)은
# 여유가 0이고, 실제로 KIS는 문서 한도의 절반쯤에서 이미 EGW00201을 돌려준다.
# 400종목 증분 수집(실전)으로 실측한 값:
#
#   pace 0.06 → EGW00201 32회, 21회   (약 5~8%),  60초
#   pace 0.12 → 6회                   (약 1.5%),  69초
#   pace 0.20 → 0회, 1회              (약 0.2%),  94초
#   pace 0.30 → 0회                              134초
#
# 임계값이 아니라 확률이다 — 같은 pace로 재측정하면 횟수가 달라진다. 그래서
# pace로는 빈도만 낮출 수 있고 없앨 수는 없다. 실제 방어는 재시도이고(어느
# 값에서도 실패 0), 0.20을 쓰는 이유는 걸릴 때마다 _RETRY_BACKOFF만큼(1초)
# 버리기 때문이다 — 0.06에서 32회면 32초를 대기로 쓰고, 그만큼 로그가 시끄러워
# 진짜 이상을 묻는다. 전체 수집(2,462종목)에서 늘어나는 시간은 몇 분 수준이고,
# 실전 전체 수집은 API가 아니라 종목당 DB 쓰기가 병목이다(41분 = 초당 1건).
#
# 모의값은 **실측하지 않았다** — daily_collect는 실전에서 돈다. 실전에서 안전한
# 지점이 문서 한도의 1/4이었으니 모의(초당 2건)도 그 비율로 내려 1.0으로 둔다.
# 모의로 대량 수집을 할 일이 생기면 위와 같은 방식으로 실측할 것.
DEFAULT_PACE_PAPER = 1.0
DEFAULT_PACE_REAL = 0.20

# 분봉은 실전 전용이고 종목당 약 980호출이다(하루 약 4호출 × 245거래일). 위
# 실전값(0.20)의 두 배로 둔다 — 프로브에서 분봉 조회가 EGW00201에 실제로
# 걸렸고, 한 종목이 오래 도는 작업이라 재시도 대기가 누적되면 전체가 밀린다.
DEFAULT_PACE_MINUTES = 0.4


def _default_pace(settings) -> float:
    return DEFAULT_PACE_PAPER if settings.paper else DEFAULT_PACE_REAL


def _run_collect_minutes(
    symbols_arg: str | None,
    from_watchlist: bool,
    days: int | None,
    pace: float | None,
    limit: int | None,
    refetch: bool = False,
) -> int:
    """분봉 수집. 일봉(`collect-prices`)과 별 커맨드다 — API·보관기간·정합성
    문제가 전부 달라서(`data/minutes.py` 참고) 옵션을 섞으면 오히려 헷갈린다."""
    from sqlalchemy.exc import SQLAlchemyError

    from sontrader.data.db import migrate
    from sontrader.data.master import load_names
    from sontrader.data.minutes import MAX_DAYS, MinuteCollectionAborted, collect_minutes_all

    try:
        settings = load_settings()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # 명세상 모의투자 미지원(주식일별분봉조회 [국내주식-213]: "모의 TR_ID
    # 모의투자 미지원"). 조용히 넘기면 KIS가 애매한 오류로 답한다.
    if settings.paper:
        print(
            "error: 분봉 조회(FHKST03010230)는 모의투자를 지원하지 않습니다. "
            "KIS_PAPER=false 로 실전 자격증명을 쓰세요.",
            file=sys.stderr,
        )
        return 2

    engine = _open_engine()
    if engine is None:
        return 2
    try:
        for action in migrate(engine):
            print(action)

        symbols = _minute_symbols(engine, symbols_arg, from_watchlist)
        if not symbols:
            print(
                "error: 수집 대상이 없습니다. --symbols 로 지정하거나 "
                "--from-watchlist 로 최신 워치리스트를 쓰세요.",
                file=sys.stderr,
            )
            return 2
        if limit is not None:
            symbols = symbols[:limit]

        # 종목명을 한 번만 읽어 둔다 — 호출마다 조회하면 수백 번 왕복한다.
        # 마스터에 없는 종목(상장폐지 등)은 코드만 보여준다.
        names = load_names(engine, symbols)

        def label(symbol: str) -> str:
            name = names.get(symbol)
            return _pad(f"{symbol} {name}" if name else symbol, 20)

        # 일봉보다 보수적으로 잡는다. 실측에서 분봉 조회가 EGW00201(초당 초과)에
        # 실제로 걸렸고, 종목당 약 980호출이라 한 번 걸릴 때마다 재시도 대기가 붙는다.
        pace_seconds = pace if pace is not None else DEFAULT_PACE_MINUTES
        # 기간 미지정이 정상 사용법 — 분봉은 1년만 보관되므로 서버가 가진
        # 만큼 전부 받고, 실제 경계는 API의 빈 응답이 알려준다.
        span = days if days is not None else MAX_DAYS
        now = _now_kst()
        print(
            f"분봉 수집 시작: {len(symbols)}종목{' (재수집)' if refetch else ''}, 과거 {span}일"
            f"{'' if days is not None else ' (서버 보관 전체)'}, "
            f"간격 {pace_seconds}초 (기준 {now:%Y-%m-%d %H:%M})"
        )

        def on_progress(index: int, total: int) -> None:
            print(f"  [{index}/{total}] {label(symbols[index - 1])} 완료", flush=True)

        def on_page(p) -> None:
            # 호출마다 한 줄. 종목당 약 980호출이라 진행률 없이는 언제 끝날지
            # 짐작할 수 없다 — 오늘 실제로 그 상태로 몇 시간을 기다렸다.
            print(
                f"    {label(p.symbol)} #{p.page:<4} {p.rows:>3}건 → {p.reached:%Y-%m-%d %H:%M}"
                f"  {p.percent:5.1f}%  누적 {p.stored:,}행",
                flush=True,
            )

        try:
            with KisClient(settings) as client:
                results, failures = collect_minutes_all(
                    engine,
                    client,
                    symbols,
                    now=now,
                    days=span,
                    pace_seconds=pace_seconds,
                    on_progress=on_progress,
                    on_page=on_page,
                    refetch=refetch,
                )
        except MinuteCollectionAborted as exc:
            print(f"error: 수집 중단 — {exc}", file=sys.stderr)
            print(f"  중단 시점까지 {len(exc.results)}종목 저장됨", file=sys.stderr)
            for symbol, failure in exc.failures[-3:]:
                print(f"  실패 {symbol}: {_first_line(failure)}", file=sys.stderr)
            return 1

        rows = sum(r.rows for r in results)
        calls = sum(r.pages for r in results)
        print(f"수집 완료: {len(results)}종목, {rows:,}행, {calls:,}호출 (실패 {len(failures)})")
        for symbol, exc in failures[:5]:
            print(f"  실패 {label(symbol)}: {_first_line(exc)}", file=sys.stderr)
        return 1 if failures else 0
    except SQLAlchemyError as exc:
        print(f"error: DB access failed: {_first_line(exc)}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()


def _minute_symbols(engine, symbols_arg: str | None, from_watchlist: bool) -> list[str]:
    """수집 대상 종목. --symbols 가 우선이고, 없으면 최신 워치리스트."""
    if symbols_arg:
        return [s.strip().zfill(6) for s in symbols_arg.split(",") if s.strip()]
    if not from_watchlist:
        return []
    import sqlalchemy as sa

    from sontrader.data.db import watchlist_snapshots

    columns = watchlist_snapshots.c
    with engine.connect() as conn:
        latest = conn.execute(sa.select(sa.func.max(columns.date))).scalar_one_or_none()
        if latest is None:
            return []
        rows = conn.execute(
            sa.select(columns.symbol).where(columns.date == latest).order_by(columns.rank)
        )
        return [row.symbol for row in rows]


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


def _run_backfill_prices(
    limit: int | None, pace: float | None, earliest_str: str | None, dry_run: bool
) -> int:
    from sqlalchemy.exc import SQLAlchemyError

    from sontrader.data.db import migrate
    from sontrader.data.master import load_collectable_symbols, load_listing_dates
    from sontrader.data.prices import backfill_daily_all

    earliest = None
    if earliest_str:
        try:
            earliest = _parse_date_arg(earliest_str)
        except ValueError:
            print(f"error: --earliest must be YYYYMMDD, got {earliest_str!r}", file=sys.stderr)
            return 2
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
            print(_empty_universe_hint(engine), file=sys.stderr)
            return 2
        if limit is not None:
            symbols = symbols[:limit]
        listing_dates = load_listing_dates(engine)

        estimate = _backfill_estimate(engine, symbols, listing_dates, earliest)
        print(estimate)
        if dry_run:
            return 0

        pace_seconds = pace if pace is not None else _default_pace(settings)

        def on_progress(index: int, total: int) -> None:
            if index % 50 == 0 or index == total:
                print(f"  {index}/{total}", flush=True)

        with KisClient(settings) as client:
            results, failures = backfill_daily_all(
                engine,
                client,
                symbols,
                listing_dates=listing_dates,
                earliest=earliest,
                pace_seconds=pace_seconds,
                on_progress=on_progress,
            )
        rows = sum(r.rows for r in results)
        pages = sum(r.pages for r in results)
        print(
            f"백필 완료: {len(results)}종목, {rows:,}행 추가, {pages:,}호출 (실패 {len(failures)})"
        )
        for symbol, exc in failures[:5]:
            print(f"  실패 {symbol}: {_first_line(exc)}", file=sys.stderr)
        return 1 if failures else 0
    except SQLAlchemyError as exc:
        print(f"error: DB access failed: {_first_line(exc)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("중단됨 — 지금까지 채운 구간은 저장되어 있고, 재실행하면 이어서 채웁니다.")
        return 0
    finally:
        engine.dispose()


def _backfill_estimate(engine, symbols, listing_dates, earliest) -> str:
    """실행 전 규모를 알린다 — 전체 백필은 몇 시간 단위라 눈으로 확인해야 한다."""
    import math

    import sqlalchemy as sa

    from sontrader.data.db import stock_candles_1d

    columns = stock_candles_1d.c
    with engine.connect() as conn:
        oldest = {
            row.symbol: row.first_date
            for row in conn.execute(
                sa.select(columns.symbol, sa.func.min(columns.date).label("first_date")).group_by(
                    columns.symbol
                )
            )
        }
    pages = 0
    covered = 0
    for symbol in symbols:
        first = oldest.get(symbol)
        if first is None:
            continue  # 아직 한 봉도 없음 — collect-prices가 먼저
        bounds = [d for d in (listing_dates.get(symbol), earliest) if d is not None]
        floor = max(bounds) if bounds else None
        if floor is None:
            continue  # 하한 미상 — 빈 페이지 연속으로 종료, 추정 불가
        span = (first - timedelta(days=1) - floor).days + 1
        if span <= 0:
            continue
        covered += 1
        pages += math.ceil(span / 100)
    hours = pages * 0.2 / 3600
    return (
        f"백필 대상 {covered:,}종목 / 예상 {pages:,}호출 / 약 {hours:.1f}시간 "
        f"(하한: {earliest or '상장일'})"
    )


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
    from sontrader.data.prices import CollectionAborted, collect_daily_all

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
        now = _now_kst()
        today = now.date()
        from sontrader.data.calendar import BAR_FINAL_AFTER, today_bar_is_final

        include_today = today_bar_is_final(now)
        if not include_today:
            print(
                f"장중({now:%H:%M})이라 오늘 봉은 저장하지 않습니다 — 임시 종가입니다. "
                f"{BAR_FINAL_AFTER:%H:%M} 이후 다시 실행하세요."
            )
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
        pace_seconds = pace if pace is not None else _default_pace(settings)

        def on_progress(index: int, total: int) -> None:
            if index % 100 == 0 or index == total:
                print(f"  {index}/{total}", flush=True)

        try:
            with KisClient(settings) as client:
                results, failures = collect_daily_all(
                    engine,
                    client,
                    symbols,
                    today=today,
                    lookback_days=lookback_days,
                    pace_seconds=pace_seconds,
                    on_progress=on_progress,
                    include_today=include_today,
                )
        except CollectionAborted as exc:
            # 공통 원인으로 전부 실패하는 상황 — 남은 종목을 계속 시도해도
            # 시간과 API 유량만 쓴다. 이미 저장된 분량은 보고한다.
            print(f"error: 수집 중단 — {exc}", file=sys.stderr)
            print(f"  중단 시점까지 {len(exc.results)}종목 저장됨", file=sys.stderr)
            for symbol, failure in exc.failures[-3:]:
                print(f"  실패 {symbol}: {_first_line(failure)}", file=sys.stderr)
            return 1
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
    date_str: str | None,
    min_trade_value: int,
    lookback: int,
    skip: int,
    from_str: str | None,
    to_str: str | None,
    scope_name: str,
) -> int:
    from sqlalchemy.exc import SQLAlchemyError

    from sontrader.data.db import migrate
    from sontrader.data.universe import UniverseError, UniverseScope, build_snapshot

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
    scope = UniverseScope.STRUCTURAL if scope_name == "structural" else UniverseScope.TRADEABLE_NOW
    engine = _open_engine()
    if engine is None:
        return 2

    if from_str or to_str:
        if not (from_str and to_str):
            print("error: --from and --to must be given together.", file=sys.stderr)
            engine.dispose()
            return 2
        try:
            return _run_build_universe_range(
                engine,
                _parse_date_arg(from_str),
                _parse_date_arg(to_str),
                lookback=lookback,
                skip=skip,
                min_trade_value=min_trade_value,
                scope=scope,
            )
        except ValueError:
            print("error: --from/--to must be YYYYMMDD", file=sys.stderr)
            return 2
        finally:
            engine.dispose()

    try:
        for action in migrate(engine):
            print(action)
        result = build_snapshot(
            engine,
            as_of=as_of or _now_kst().date(),
            lookback=lookback,
            skip=skip,
            min_avg_trade_value=min_trade_value,
            scope=scope,
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


def _run_build_universe_range(
    engine, start, end, *, lookback: int, skip: int, min_trade_value: int, scope
) -> int:
    """기간 내 모든 거래일의 스냅샷을 **연대순으로** 만든다.

    순서가 중요하다: 히스테리시스(편입 50 / 이탈 70)가 직전 스냅샷을 참조하므로,
    거꾸로 만들거나 건너뛰면 이후 전부가 어긋난다. 그래서 기존 스냅샷이 구간
    안에 남아 있으면 먼저 지운다 — 다른 파라미터로 만든 잔재가 체인에 섞이면
    재현성이 깨진다.

    거래일 목록은 저장된 일봉에서 가져온다. 캘린더를 따로 두지 않아도 되고,
    "데이터가 있는 날"과 "스냅샷이 있는 날"이 정확히 일치하게 된다.
    """
    import time as time_module

    import sqlalchemy as sa
    from sqlalchemy.exc import SQLAlchemyError

    from sontrader.data.db import migrate, stock_candles_1d, watchlist_snapshots
    from sontrader.data.universe import UniverseError, build_snapshot

    try:
        for action in migrate(engine):
            print(action)
        columns = stock_candles_1d.c
        with engine.connect() as conn:
            days = [
                row.date
                for row in conn.execute(
                    sa.select(columns.date)
                    .where(columns.date >= start, columns.date <= end)
                    .distinct()
                    .order_by(columns.date)
                )
            ]
        if not days:
            print(f"error: {start} ~ {end} 구간에 일봉이 없습니다.", file=sys.stderr)
            return 2

        with engine.begin() as conn:
            deleted = conn.execute(
                sa.delete(watchlist_snapshots).where(
                    watchlist_snapshots.c.date >= start, watchlist_snapshots.c.date <= end
                )
            ).rowcount
        if deleted:
            print(f"기존 스냅샷 {deleted}행 삭제 (히스테리시스 체인 재구성)")

        print(f"{len(days)}거래일 생성 시작 ({days[0]} ~ {days[-1]}, scope={scope.value})")
        started = time_module.time()
        empty = 0
        for index, day in enumerate(days, start=1):
            result = build_snapshot(
                engine,
                as_of=day,
                lookback=lookback,
                skip=skip,
                min_avg_trade_value=min_trade_value,
                scope=scope,
            )
            if not result.entries:
                empty += 1
            if index % 100 == 0 or index == len(days):
                elapsed = time_module.time() - started
                eta = elapsed / index * (len(days) - index)
                print(
                    f"  {index}/{len(days)}  {day}  워치 {len(result.entries)}종목"
                    f"  (경과 {elapsed / 60:.0f}분, 남은 {eta / 60:.0f}분)",
                    flush=True,
                )
        print(f"완료: {len(days)}거래일 (워치리스트가 빈 날 {empty}일)")
        return 0
    except UniverseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except SQLAlchemyError as exc:
        print(f"error: DB access failed: {_first_line(exc)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("중단됨 — 재실행하면 구간 전체를 처음부터 다시 만듭니다(체인 정합성 유지).")
        return 0


def _build_cached_judge(engine, model: str):
    """DB에 이미 저장된 LLM 판단만 읽어 오는 judge — API를 호출하지 않는다.

    백테스트는 LLM을 부르지 않는다. 부르는 순간 (1) 같은 구간을 두 번 돌린
    결과가 달라질 수 있고, (2) 그 시점에 존재하지 않았던 모델의 판단이
    과거 구간에 섞인다. 판단은 실전 루프(`apps/live.py`)가 그때그때
    `llm_judgments`에 남긴 것만 쓴다.

    캐시에 없는 이벤트는 `None`(진입 안 함)으로 넘긴다. 호출자가 미스 건수를
    셀 수 있도록 카운터를 함께 돌려준다 — 캐시가 비어 있으면 신규 진입이
    0건이 되는데, 그게 전략의 결론인지 데이터가 없어서인지 구분되어야 한다.
    """
    from sontrader.llm import cache
    from sontrader.llm.judge import PROMPT_VERSION

    misses = {"n": 0}

    def judge(event):
        cached = cache.load(engine, event.event_id, PROMPT_VERSION, model)
        if cached is None:
            misses["n"] += 1
        return cached

    return judge, misses


def _build_cycle_config(
    entry_trigger: str,
    cooldown_days: int | None,
    stop_basis: str = "close",
):
    """진입 트리거와 쿨다운만 바꾼 사이클 설정.

    워치리스트 모드는 event_id가 없어 게이트의 동일이벤트 차단이 걸리지
    않는다 — 청산 직후 같은 종목이 여전히 상위면 바로 재진입한다. 그래서
    이 모드의 유일한 제동은 쿨다운이고, 값을 명시할 수 있게 열어둔다.
    """
    from sontrader.core.gate import GateConfig
    from sontrader.core.strategy import EntryTrigger, StrategyConfig
    from sontrader.core.types import ExitRule, StopBasis
    from sontrader.engine.loop import CycleConfig

    trigger = EntryTrigger.WATCHLIST_RANK if entry_trigger == "watchlist" else EntryTrigger.EVENT
    # 워치리스트 모드 진입에 붙일 청산 조건. EVENT 모드는 LLM이 규칙을 실어
    # 보내므로 여기서 정하지 않는다.
    config = CycleConfig(
        strategy=StrategyConfig(
            entry_trigger=trigger,
            exit_rule=ExitRule(stop_basis=StopBasis(stop_basis)),
        )
    )
    if cooldown_days is not None:
        config = replace(config, gate=GateConfig(cooldown_days=cooldown_days))
    return config


def _run_backtest(
    start_str: str,
    end_str: str,
    initial_cash: int,
    use_llm: bool,
    llm_model: str,
    entry_trigger: str,
    cooldown_days: int | None,
    slippage_bps: float | None = None,
    stop_basis: str = "close",
) -> int:
    from sqlalchemy.exc import SQLAlchemyError

    from sontrader.adapters.broker_sim import SimBrokerConfig
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
    if use_llm and entry_trigger != "event":
        # 조용히 무시하면 "LLM을 켠 결과"라고 믿고 해석하게 된다.
        # watchlist 모드는 판단을 보지 않으므로 조합 자체가 성립하지 않는다.
        print(
            "error: --llm은 --entry-trigger event 와 함께 써야 합니다 "
            f"(지금은 {entry_trigger}: 워치리스트 순위만 보고 LLM 판단은 무시됩니다).",
            file=sys.stderr,
        )
        return 2

    engine = _open_engine()
    if engine is None:
        return 2
    try:
        for action in migrate(engine):
            print(action)
        judge = None
        misses = None
        if use_llm:
            judge, misses = _build_cached_judge(engine, llm_model)
        cycle_config = _build_cycle_config(entry_trigger, cooldown_days, stop_basis)
        if stop_basis != "close":
            print(f"스톱 판정 기준: 봉 {stop_basis} (기본 close)")
        if entry_trigger == "watchlist":
            print("진입 트리거: 워치리스트 순위 (이벤트·LLM 미사용)")
        elif judge is None:
            print("주의: --llm 미지정 — 이번 실행은 신규 진입 없이 청산 로직만 검증합니다.")
        else:
            print(f"진입 판단: 저장된 LLM 판단만 사용 (model={llm_model}, API 호출 없음)")
        broker_config = None
        if slippage_bps is not None:
            # 자리표시자(10bp)에 결론이 얼마나 기대고 있는지 재기 위한 손잡이다.
            # 실측치가 나오기 전까지 기본값은 바꾸지 않는다 — 근거 없는 숫자를
            # 기본값에 넣으면 그게 곧 은닉된 전략 결정이 된다.
            broker_config = SimBrokerConfig(slippage_bps=slippage_bps)
            print(f"슬리피지 {slippage_bps:g}bp (기본 {SimBrokerConfig().slippage_bps:g}bp 대체)")
        result = run_backtest(
            engine,
            start=start,
            end=end,
            initial_cash=initial_cash,
            judge=judge,
            broker_config=broker_config,
            cycle_config=cycle_config,
        )
        final_equity = result.equity_curve[-1][1] if result.equity_curve else initial_cash
        print(f"{start} ~ {end}: 사이클 {len(result.equity_curve)}일, 체결 {len(result.fills)}건")
        print(
            f"최종 현금 {result.final_cash:,}원, 보유 {len(result.final_positions)}종목,"
            f" 최종 평가자산 {final_equity:,}원"
        )
        if result.rejections:
            print(f"거부 {len(result.rejections)}건 (슬롯/중복이벤트/쿨다운 등)")
        if misses and misses["n"]:
            # 캐시 미스를 조용히 넘기면 "신규 진입 0건"이 전략의 결론인지
            # 판단 데이터가 없어서인지 구분되지 않는다.
            print(
                f"주의: 저장된 판단이 없는 이벤트 {misses['n']}건 — 그만큼 진입 후보에서"
                f" 빠졌습니다 (model={llm_model})."
            )

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


def _run_slippage(since_str: str | None) -> int:
    """실측 슬리피지를 출력한다. 표본이 없으면 **없다고 말한다.**

    0.0을 찍으면 "슬리피지가 없다"로 읽혀 자리표시자보다 나쁜 거짓말이 된다.
    """
    from sqlalchemy.exc import SQLAlchemyError

    from sontrader.apps.slippage import SlippageReport, load_live_samples

    since = None
    if since_str is not None:
        try:
            since = _parse_date_arg(since_str)
        except ValueError:
            print("error: --since must be YYYYMMDD", file=sys.stderr)
            return 2

    engine = _open_engine()
    if engine is None:
        return 2
    try:
        report = SlippageReport.of(load_live_samples(engine, since=since))
    except SQLAlchemyError as exc:
        print(f"error: DB access failed: {_first_line(exc)}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()

    if report.overall.sample_size == 0:
        print("표본 0건 — 실측 슬리피지를 계산할 수 없습니다.")
        print(
            "  기준가(orders.ref_price)와 체결(fills)이 둘 다 있는 주문이 필요합니다. "
            "실전/모의 매매를 돌려 체결이 쌓이면 그때 다시 실행하세요."
        )
        return 0

    print("실측 슬리피지 — 의사결정 기준가 대비 체결가 (+ = 불리)")
    print("주의: 순수 슬리피지가 아니라 집행 손실입니다 — 의사결정~체결 사이의")
    print("      가격 변동(밤샘 갭 등)이 섞여 있습니다 (apps/slippage.py 참고).")
    for label, stats in (("전체", report.overall), ("매수", report.buys), ("매도", report.sells)):
        if stats.sample_size == 0:
            print(f"  {label}: 표본 없음")
            continue
        print(
            f"  {label}: n={stats.sample_size} "
            f"중앙값 {stats.median_bps:+.1f}bp  평균 {stats.mean_bps:+.1f}bp  "
            f"수량가중 {stats.qty_weighted_mean_bps:+.1f}bp  "
            f"p90 {stats.p90_bps:+.1f}bp  최악 {stats.worst_bps:+.1f}bp"
        )
    return 0


def _pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value:+.2%}"


def _num(value: float | None, suffix: str = "") -> str:
    return "N/A" if value is None else f"{value:.2f}{suffix}"


def _print_report(report) -> None:
    from sontrader.apps.report import MIN_TRADE_SAMPLE

    print(f"CAGR {_pct(report.cagr)}  샤프 {_num(report.sharpe)}  MDD {report.mdd:.2%}")
    print(
        f"승률 {_pct(report.win_rate)}  PF {_num(report.profit_factor)}"
        f"  손익비 {_num(report.payoff_ratio)}"
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
        "--pace", type=float, default=None, help="API 호출 간격 초 (기본: 모의 1.0, 실전 0.2)"
    )
    prices.add_argument(
        "--lookback-days", type=int, default=420, help="최초 수집 시 소급 일수 (달력일)"
    )

    minutes = sub.add_parser(
        "collect-minutes",
        help="분봉 수집 (거래소 공식 1분봉, 백테스트용 — 실전 자격증명 필수)",
    )
    minutes.add_argument(
        "--symbols", default=None, help="쉼표로 구분한 종목코드 (예: 005930,000660)"
    )
    minutes.add_argument(
        "--from-watchlist",
        action="store_true",
        help="최신 워치리스트 스냅샷의 종목을 순위대로 수집",
    )
    minutes.add_argument(
        "--days",
        type=int,
        default=None,
        help="과거 소급 일수 (기본: 서버 보관 전체 ≈1년). 짧게 시험할 때만 지정",
    )
    minutes.add_argument("--pace", type=float, default=None, help="API 호출 간격 초 (기본 0.4)")
    minutes.add_argument("--limit", type=int, default=None, help="상위 N종목만 (테스트용)")
    minutes.add_argument(
        "--refetch",
        action="store_true",
        help="저장분을 무시하고 구간 전체를 다시 받는다 (구멍 메우기 / 과거 확장)",
    )

    backfill = sub.add_parser(
        "backfill-prices",
        help="일봉을 과거 방향으로 채운다 (collect-prices는 앞으로만 간다)",
    )
    backfill.add_argument(
        "--earliest", help="이 날짜(YYYYMMDD) 이전은 받지 않는다. 생략하면 상장일까지"
    )
    backfill.add_argument("--limit", type=int, default=None, help="상위 N종목만 (테스트용)")
    backfill.add_argument("--pace", type=float, default=None, help="API 호출 간격 초")
    backfill.add_argument("--dry-run", action="store_true", help="규모만 추정하고 종료 (호출 없음)")

    universe = sub.add_parser(
        "build-universe", help="모멘텀 워치리스트 산출 + 일별 스냅샷 저장 (히스테리시스 30/42)"
    )
    universe.add_argument("--date", help="기준일 YYYYMMDD (기본: 오늘)")
    universe.add_argument(
        "--min-trade-value",
        type=int,
        default=10_000_000_000,
        help="최근 20거래일 평균 거래대금 하한 (KRW, 기본 100억)",
    )
    universe.add_argument("--lookback", type=int, default=252, help="모멘텀 룩백 (거래일)")
    universe.add_argument("--from", dest="from_date", help="배치 시작일 YYYYMMDD (--to와 함께)")
    universe.add_argument("--to", dest="to_date", help="배치 종료일 YYYYMMDD (--from과 함께)")
    universe.add_argument(
        "--universe-scope",
        choices=["tradeable", "structural"],
        default="tradeable",
        help="마스터 필터 기준. tradeable=오늘 상태 플래그 포함(기본), "
        "structural=구조적 속성만 (과거 소급 생성용 — 생존 편향 감소)",
    )
    universe.add_argument("--skip", type=int, default=21, help="모멘텀 스킵 (거래일)")

    backtest = sub.add_parser("backtest", help="백테스트 실행")
    backtest.add_argument("--start", required=True, help="시작일 YYYYMMDD")
    backtest.add_argument("--end", required=True, help="종료일 YYYYMMDD")
    backtest.add_argument(
        "--entry-trigger",
        choices=["watchlist", "event"],
        default="watchlist",
        help="진입 촉발 조건. watchlist=워치리스트 순위만 (기본, LLM 미개입), "
        "event=공시 + 저장된 LLM 판단",
    )
    backtest.add_argument(
        "--cooldown-days",
        type=int,
        default=None,
        help="청산 후 같은 종목 재진입 금지 일수. watchlist 모드의 유일한 재진입 제동",
    )
    backtest.add_argument(
        "--initial-cash", type=int, default=10_000_000, help="초기 자본 KRW (기본 1,000만원)"
    )
    backtest.add_argument(
        "--llm",
        action="store_true",
        help="저장된 LLM 판단(llm_judgments)으로 진입 판단; "
        "생략하면 신규 진입 없이 청산 로직만 검증. API는 호출하지 않는다",
    )
    backtest.add_argument(
        "--llm-model",
        default="claude-opus-5",
        help="어느 모델의 판단을 읽을지 (llm_judgments 캐시 키의 일부)",
    )
    backtest.add_argument(
        "--slippage-bps",
        type=float,
        default=None,
        help="체결 슬리피지 bp. 생략하면 기본 자리표시자(10bp). "
        "실측치가 없어 결론이 이 값에 얼마나 민감한지 재는 용도 (docs/system/02)",
    )
    backtest.add_argument(
        "--stop-basis",
        choices=["close", "low"],
        default="close",
        help="스톱 이탈을 봉의 어느 가격으로 판정할지. close=종가(기본), "
        "low=저가(장중에 스톱을 건드리면 이탈). 설계 4절의 '종가 기준'은 분봉을 "
        "전제한 문장이라 일봉에서는 의미가 다르다 (core/types.py StopBasis)",
    )

    slippage = sub.add_parser(
        "slippage", help="실측 슬리피지 — orders.ref_price 대비 실제 체결가 분포"
    )
    slippage.add_argument("--since", default=None, help="이 날짜 이후 체결만 YYYYMMDD")

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
    if args.command == "collect-minutes":
        return _run_collect_minutes(
            args.symbols, args.from_watchlist, args.days, args.pace, args.limit, args.refetch
        )
    if args.command == "collect-prices":
        return _run_collect_prices(args.limit, args.pace, args.lookback_days)
    if args.command == "backfill-prices":
        return _run_backfill_prices(args.limit, args.pace, args.earliest, args.dry_run)
    if args.command == "build-universe":
        return _run_build_universe(
            args.date,
            args.min_trade_value,
            args.lookback,
            args.skip,
            args.from_date,
            args.to_date,
            args.universe_scope,
        )
    if args.command == "backtest":
        return _run_backtest(
            args.start,
            args.end,
            args.initial_cash,
            args.llm,
            args.llm_model,
            args.entry_trigger,
            args.cooldown_days,
            args.slippage_bps,
            args.stop_basis,
        )
    if args.command == "slippage":
        return _run_slippage(args.since)

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
