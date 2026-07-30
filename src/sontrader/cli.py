"""Command-line interface for quick manual access to quotes, balance, and orders."""

from __future__ import annotations

import argparse
import sys

from sontrader.client import KisClient, KisError
from sontrader.config import load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sontrader", description="Korean stock trading via the KIS open API"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    quote = sub.add_parser("quote", help="현재가 조회")
    quote.add_argument("code", help="6-digit ticker, e.g. 005930")

    sub.add_parser("balance", help="계좌 잔고 조회")

    for side, korean in (("buy", "매수"), ("sell", "매도")):
        order = sub.add_parser(side, help=f"{korean} 주문")
        order.add_argument("code", help="6-digit ticker")
        order.add_argument("quantity", type=int)
        order.add_argument(
            "--price", type=int, default=None, help="limit price in KRW; omit for a market order"
        )

    args = parser.parse_args(argv)
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
