"""Thin wrapper around the KIS domestic-stock REST API.

Each public method maps to one KIS endpoint. tr_id values differ
between the real and paper (모의투자) environments; the mapping lives
in _TR_IDS. API reference: https://apiportal.koreainvestment.com
"""

from __future__ import annotations

import logging
import time as time_module
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import httpx

from sontrader.auth import KisError, TokenManager, is_transient, raise_for_kis_error
from sontrader.config import Settings

log = logging.getLogger(__name__)

# endpoint key -> (real tr_id, paper tr_id)
# https://apiportal.koreainvestment.com/apiservice-apiservice?/uapi/domestic-stock/v1/trading/order-cash
_TR_IDS = {
    "quote": ("FHKST01010100", "FHKST01010100"),
    "daily": ("FHKST03010100", "FHKST03010100"),
    "balance": ("TTTC8434R", "VTTC8434R"),
    "buy": ("TTTC0012U", "VTTC0012U"),
    "sell": ("TTTC0011U", "VTTC0011U"),
    # 주식일별주문체결조회, 3개월 이내 기준. docs/api/주식일별주문체결조회[v1_국내주식-005].xlsx
    "daily_ccld": ("TTTC0081R", "VTTC0081R"),
    # 주식일별분봉조회(과거 분봉, 최대 1년 보관). 모의투자 미지원 —
    # get_intraday_candles()가 호출 전에 막는다. docs/api/주식일별분봉조회[국내주식-213].xlsx
    "intraday": ("FHKST03010230", ""),
    # 국내휴장일조회. 모의투자 미지원. docs/api/국내휴장일조회[국내주식-040].xlsx
    "holidays": ("CTCA0903R", ""),
    # 국내업종 일자별지수 (R22). **모의투자 지원**(2026-08-27 실측 rt_cd=0).
    # 응답은 요청 구간과 무관하게 **최신 50건 상한**이라 페이징이 필요하다.
    "index_daily": ("FHKUP03500100", "FHKUP03500100"),
}

ORDER_DVSN_LIMIT = "00"  # 지정가
ORDER_DVSN_MARKET = "01"  # 시장가


_RETRIES = 3
_RETRY_BACKOFF = 0.6  # 초. 모의투자 초당 2건 한도를 한 번의 대기로 넘긴다

__all__ = ["KisClient", "KisError"]


class KisClient:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.BaseTransport | None = None,
        *,
        sleep: Callable[[float], None] = time_module.sleep,
    ):
        self._settings = settings
        # read를 connect보다 길게 잡는다. 잔고조회(TTTC8434R)는 KIS 원장을 타서
        # 장 시작·마감 직후 응답이 수 초로 튀는데, 전체 10초로는 서버가 살아
        # 있는데도 ReadTimeout이 났다.
        self._http = httpx.Client(
            base_url=settings.base_url,
            timeout=httpx.Timeout(10.0, read=20.0),
            transport=transport,
        )
        self._tokens = TokenManager(settings, self._http)
        self._sleep = sleep

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

    def get_intraday_candles(self, code: str, reference: datetime) -> list[dict[str, Any]]:
        """주식일별분봉조회 — ``reference`` 시각부터 과거로 최대 120건(최대 1년 보관).

        모의투자를 지원하지 않는다(TR_ID FHKST03010230는 실전 전용) — 호출
        전에 명확히 실패시킨다. 조용히 넘기면 KIS가 애매한 오류로 답한다.

        응답(output2)은 최신이 먼저인 **내림차순**이다(``get_daily_candles``와
        반대). 120건을 넘는 과거 데이터가 필요하면, 이 응답의 가장 오래된
        행의 날짜·시각을 다음 호출의 ``reference``로 넘겨 페이징한다
        (호출자 몫 — ``get_daily_candles``의 "페이징은 호출자 몫"과 같은 이유).
        """
        if self._settings.paper:
            raise KisError("주식일별분봉조회(FHKST03010230)는 모의투자를 지원하지 않습니다")
        data = self._request(
            "GET",
            "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice",
            tr="intraday",
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": code,
                "FID_INPUT_DATE_1": reference.strftime("%Y%m%d"),
                "FID_INPUT_HOUR_1": reference.strftime("%H%M%S"),
                "FID_PW_DATA_INCU_YN": "Y",
                "FID_FAKE_TICK_INCU_YN": "",
            },
        )
        return [row for row in data["output2"] if row.get("stck_bsop_date")]

    def get_index_daily_candles(self, code: str, start: date, end: date) -> list[dict[str, Any]]:
        """국내업종 일자별지수 (`FHKUP03500100`). 시각 오름차순으로 돌려준다.

        **응답은 최신 50건 상한**이다 — 요청 구간이 그보다 길면 **뒤쪽(최신)
        50건만** 온다. 앞쪽을 받으려면 `end`를 뒤로 물려 다시 부른다
        (페이징은 호출자 몫 — `get_daily_candles`와 같은 규약).

        지수값은 **소수점을 갖는다**(6912.37). 정수로 캐스팅하지 말 것.
        """
        data = self._request(
            "GET",
            "/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice",
            tr="index_daily",
            params={
                # "U" = 업종. 종목 조회의 "J"(주식)와 다르다.
                "FID_COND_MRKT_DIV_CODE": "U",
                "FID_INPUT_ISCD": code,
                "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
                "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE": "D",
            },
        )
        rows = [row for row in (data.get("output2") or []) if row.get("stck_bsop_date")]
        rows.sort(key=lambda r: r["stck_bsop_date"])
        return rows

    def get_market_holidays(self, reference: date) -> list[dict[str, Any]]:
        """국내휴장일조회 — ``reference``부터 몇 주치 영업일 정보를 한 번에 돌려준다.

        모의투자를 지원하지 않는다(TR_ID CTCA0903R는 실전 전용) — 호출 전에
        명확히 실패시킨다.

        KIS 문서가 "원장서비스와 연관돼 있어 가급적 1일 1회 호출"을 명시적으로
        요청한다 — 이 메서드 자체는 그 제약을 강제하지 않는다(단순 REST
        래퍼). 캐시해서 호출 빈도를 낮추는 일은 ``data/calendar.py``의 몫이다.

        각 행의 ``opnd_yn``이 "개장일여부"다 — 문서: "주문을 넣고자 할 경우
        개장일여부(opnd_yn)를 사용".
        """
        if self._settings.paper:
            raise KisError("국내휴장일조회(CTCA0903R)는 모의투자를 지원하지 않습니다")
        data = self._request(
            "GET",
            "/uapi/domestic-stock/v1/quotations/chk-holiday",
            tr="holidays",
            params={
                "BASS_DT": reference.strftime("%Y%m%d"),
                "CTX_AREA_NK": "",
                "CTX_AREA_FK": "",
            },
        )
        return data["output"]

    def get_balance(self) -> dict[str, Any]:
        """계좌 잔고: returns {"holdings": [...], "summary": {...}}.

        ``INQR_DVSN="01"``(대출일별) — 문서(주식잔고조회 v1_국내주식-006)에
        정의된 값이 이것 하나뿐이다.
        """
        data = self._request(
            "GET",
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            tr="balance",
            params={
                "CANO": self._settings.cano,
                "ACNT_PRDT_CD": self._settings.acnt_prdt_cd,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "01",
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

    def get_daily_executions(
        self,
        start: date,
        end: date,
        *,
        symbol: str | None = None,
        broker_order_no: str | None = None,
    ) -> list[dict[str, Any]]:
        """주식일별주문체결조회. 최근 3개월 이내 기준(TTTC0081R/VTTC0081R) —
        그 이전 체결은 별도 TR_ID(CTSC9215R 계열)가 필요하며 여기서는 다루지
        않는다(자가 매매 상태 확인용이라 항상 최근 주문만 조회하면 된다).

        ``broker_order_no``(ODNO)를 넘기면 그 주문 하나로 좁혀진다. 한 번의
        호출에 최대 100건(모의 15건)까지 오고 그 이상은 연속조회가 필요한데,
        지금은 페이지네이션을 구현하지 않는다 — ODNO로 좁힌 조회는 결과가
        한 건뿐이라 필요 없다.
        """
        data = self._request(
            "GET",
            "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
            tr="daily_ccld",
            params={
                "CANO": self._settings.cano,
                "ACNT_PRDT_CD": self._settings.acnt_prdt_cd,
                "INQR_STRT_DT": start.strftime("%Y%m%d"),
                "INQR_END_DT": end.strftime("%Y%m%d"),
                "SLL_BUY_DVSN_CD": "00",
                "PDNO": symbol or "",
                "ORD_GNO_BRNO": "",
                "ODNO": broker_order_no or "",
                "CCLD_DVSN": "00",
                "INQR_DVSN": "00",
                "INQR_DVSN_1": "",
                "INQR_DVSN_3": "00",
                "EXCG_ID_DVSN_CD": "KRX",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
        return data["output1"]

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
        # 일시 오류는 여기서 재시도한다. 유량 한도가 모의 초당 2건이라
        # reconcile()처럼 여러 엔드포인트를 연달아 부르는 호출자가 정상 동작
        # 중에도 EGW00201을 맞는다 — 호출자마다 재시도를 붙이는 대신 모든
        # 요청이 지나는 이 지점 하나에 둔다.
        #
        # 주문에도 안전하다: `auth.TRANSIENT_ERROR_CODES`는 전부 KIS가 주문을
        # **접수하기 전에** 거절한 경우다.
        for attempt in range(1, _RETRIES + 1):
            try:
                response = self._http.request(
                    method, path, headers=headers, params=params, json=json
                )
            except httpx.TransportError:
                # 응답을 아예 못 받은 경우(ReadTimeout 등)는 **조회만** 재시도한다.
                # KIS가 요청을 받았는지 알 수 없으니 POST(주문)를 재전송하면 중복
                # 체결 위험이 있다 — 그쪽은 예외를 그대로 올려 `broker_kis.submit()`
                # 의 UNKNOWN 경로가 "접수 불명"으로 처리한다.
                #
                # 조회에서 이 재시도가 필요한 이유: 60초 주기로 상시 가동하면
                # 장중 1,000건 이상을 부르는데, 그중 한 번의 ReadTimeout이
                # live.py의 매매 루프를 그대로 죽였다.
                if method != "GET" or attempt == _RETRIES:
                    raise
                self._sleep(_RETRY_BACKOFF * attempt)
                continue
            try:
                # 상태 코드보다 본문을 먼저 본다 — 이유는 raise_for_kis_error() 참고.
                raise_for_kis_error(response)
            except KisError as exc:
                if not is_transient(exc) or attempt == _RETRIES:
                    raise
                # 자동 복구되지만 반복되면 유량 설계가 틀렸다는 뜻이다(§6.6.2 WARN).
                # 남기지 않으면 "느려진 이유"를 나중에 되짚을 방법이 없다.
                log.warning(
                    "일시 오류 재시도 %d/%d — %s %s: %s", attempt, _RETRIES, method, path, exc
                )
                self._sleep(_RETRY_BACKOFF * attempt)
                continue
            response.raise_for_status()
            return response.json()
        raise AssertionError("unreachable")
