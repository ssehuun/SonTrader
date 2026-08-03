"""Thin wrapper around the KIS domestic-stock REST API.

Each public method maps to one KIS endpoint. tr_id values differ
between the real and paper (모의투자) environments; the mapping lives
in _TR_IDS. API reference: https://apiportal.koreainvestment.com
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx

from sontrader.auth import TokenManager
from sontrader.config import Settings

# endpoint key -> (real tr_id, paper tr_id)
_TR_IDS = {
    "quote": ("FHKST01010100", "FHKST01010100"),
    "daily": ("FHKST03010100", "FHKST03010100"),
    "balance": ("TTTC8434R", "VTTC8434R"),
    "buy": ("TTTC0802U", "VTTC0802U"),
    "sell": ("TTTC0801U", "VTTC0801U"),
}

ORDER_DVSN_LIMIT = "00"  # 지정가
ORDER_DVSN_MARKET = "01"  # 시장가


class KisError(RuntimeError):
    """KIS answered with rt_cd != 0 (API-level failure)."""


class KisClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self._settings = settings
        self._http = httpx.Client(base_url=settings.base_url, timeout=10.0, transport=transport)
        self._tokens = TokenManager(settings, self._http)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> KisClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get_quote(self, code: str) -> dict[str, Any]:
        """현재가 시세. ``code`` is a 6-digit ticker like 005930."""
        data = self._request(
            "GET",
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            tr="quote",
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
        )
        return data["output"]

    def get_daily_candles(
        self, code: str, start: date, end: date, adjusted: bool = True
    ) -> list[dict[str, Any]]:
        """일봉 (국내주식기간별시세). 한 호출에 최대 100건 — 페이징은 호출자 몫.

        ``adjusted=True``면 수정주가(FID_ORG_ADJ_PRC="0") 기준이다. KIS는
        output2를 빈 dict로 패딩할 수 있어 영업일 행만 돌려준다.
        """
        data = self._request(
            "GET",
            "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            tr="daily",
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": code,
                "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
                "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE": "D",
                "FID_ORG_ADJ_PRC": "0" if adjusted else "1",
            },
        )
        return [row for row in data["output2"] if row.get("stck_bsop_date")]

    def get_balance(self) -> dict[str, Any]:
        """계좌 잔고: returns {"holdings": [...], "summary": {...}}."""
        data = self._request(
            "GET",
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            tr="balance",
            params={
                "CANO": self._settings.cano,
                "ACNT_PRDT_CD": self._settings.acnt_prdt_cd,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
        summary = data["output2"][0] if data["output2"] else {}
        return {"holdings": data["output1"], "summary": summary}

    def order(
        self, side: str, code: str, quantity: int, price: int | None = None
    ) -> dict[str, Any]:
        """현금 주문. ``price=None`` places a market (시장가) order."""
        if side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")
        data = self._request(
            "POST",
            "/uapi/domestic-stock/v1/trading/order-cash",
            tr=side,
            json={
                "CANO": self._settings.cano,
                "ACNT_PRDT_CD": self._settings.acnt_prdt_cd,
                "PDNO": code,
                "ORD_DVSN": ORDER_DVSN_MARKET if price is None else ORDER_DVSN_LIMIT,
                "ORD_QTY": str(quantity),
                "ORD_UNPR": "0" if price is None else str(price),
            },
        )
        return data["output"]

    def _request(
        self,
        method: str,
        path: str,
        *,
        tr: str,
        params: dict[str, str] | None = None,
        json: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        real_id, paper_id = _TR_IDS[tr]
        headers = {
            "authorization": f"Bearer {self._tokens.get_token()}",
            "appkey": self._settings.app_key,
            "appsecret": self._settings.app_secret,
            "tr_id": paper_id if self._settings.paper else real_id,
            "custtype": "P",
        }
        response = self._http.request(method, path, headers=headers, params=params, json=json)
        response.raise_for_status()
        data = response.json()
        if data.get("rt_cd") != "0":
            raise KisError(f"{data.get('msg_cd')}: {data.get('msg1', '').strip()}")
        return data
