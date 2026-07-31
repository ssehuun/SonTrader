"""Command-line interface for quick manual access to quotes, balance, and orders."""

from __future__ import annotations

import argparse
import sys

from sontrader.client import KisClient, KisError
from sontrader.config import load_database_url, load_settings


def _run_migrate() -> int:
    # KIS-only commands must not pay for (or depend on) SQLAlchemy, so the
    # DB stack is imported only when the migrate command actually runs.
    from sqlalchemy.exc import ArgumentError, SQLAlchemyError

    from sontrader.data.db import get_engine, migrate

    database_url = load_database_url()
    if not database_url:
        print("error: DATABASE_URL is not set. See env.example.", file=sys.stderr)
        return 2
    try:
        engine = get_engine(database_url)
    except ArgumentError:
        # Never echo the URL back: it can embed the database password.
        print("error: DATABASE_URL is not a valid SQLAlchemy URL.", file=sys.stderr)
        return 2
    try:
        actions = migrate(engine)
    except SQLAlchemyError as exc:
        detail = str(exc).split("\n", 1)[0]
        print(f"error: migration failed: {type(exc).__name__}: {detail}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    for action in actions:
        print(action)
    if not actions:
        print("schema up to date — nothing to do")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sontrader", description="Korean stock trading via the KIS open API"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    quote = sub.add_parser("quote", help="현재가 조회")
    quote.add_argument("code", help="6-digit ticker, e.g. 005930")

    sub.add_parser("balance", help="계좌 잔고 조회")

    sub.add_parser("migrate", help="DB 스키마 생성/확장 (매매 상태 테이블 + 수정주가 컬럼)")

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
