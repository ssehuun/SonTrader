"""Command-line interface for quick manual access to quotes, balance, and orders."""

from __future__ import annotations

import argparse
import sys

from sontrader.client import KisClient, KisError
from sontrader.config import load_dart_api_key, load_database_url, load_settings


def _first_line(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc).split(chr(10), 1)[0]}"


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
    from datetime import datetime, timedelta, timezone

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
            fixed_day = datetime.strptime(date_str, "%Y%m%d").date()
        except ValueError:
            print(f"error: --date must be YYYYMMDD, got {date_str!r}", file=sys.stderr)
            return 2
    if interval is not None and interval < 1:
        print("error: --interval must be >= 1 (seconds).", file=sys.stderr)
        return 2

    kst = timezone(timedelta(hours=9))

    def now_kst() -> datetime:
        # 저장 시각은 naive KST 통일 (스키마 규약).
        return datetime.now(kst).replace(tzinfo=None)

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
