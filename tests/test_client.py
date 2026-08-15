import json
from dataclasses import replace

import httpx
import pytest

from sontrader.client import KisClient, KisError
from tests.conftest import TOKEN_RESPONSE


def make_client(settings, responder):
    """KisClient whose transport answers /oauth2/tokenP itself and delegates the rest."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/tokenP":
            return httpx.Response(200, json=TOKEN_RESPONSE)
        return responder(request)

    return KisClient(settings, transport=httpx.MockTransport(handler))


def test_get_quote(settings):
    def responder(request):
        assert request.url.path == "/uapi/domestic-stock/v1/quotations/inquire-price"
        assert request.url.params["FID_INPUT_ISCD"] == "005930"
        assert request.headers["tr_id"] == "FHKST01010100"
        assert request.headers["authorization"] == "Bearer test-token"
        return httpx.Response(
            200,
            json={
                "rt_cd": "0",
                "output": {"stck_prpr": "71000", "prdy_vrss": "500", "prdy_ctrt": "0.71"},
            },
        )

    with make_client(settings, responder) as client:
        quote = client.get_quote("005930")
    assert quote["stck_prpr"] == "71000"


def test_get_daily_candles_requests_adjusted_prices(settings):
    from datetime import date

    def responder(request):
        assert request.url.path == "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
        assert request.headers["tr_id"] == "FHKST03010100"
        assert request.url.params["FID_ORG_ADJ_PRC"] == "0"  # 수정주가
        assert request.url.params["FID_INPUT_DATE_1"] == "20260701"
        assert request.url.params["FID_INPUT_DATE_2"] == "20260731"
        return httpx.Response(
            200,
            json={
                "rt_cd": "0",
                "output1": {"hts_kor_isnm": "삼성전자"},
                "output2": [
                    {"stck_bsop_date": "20260731", "stck_clpr": "71000"},
                    {},  # KIS는 빈 dict로 패딩할 수 있다
                ],
            },
        )

    with make_client(settings, responder) as client:
        rows = client.get_daily_candles("005930", date(2026, 7, 1), date(2026, 7, 31))
    assert len(rows) == 1
    assert rows[0]["stck_clpr"] == "71000"


def test_get_intraday_candles_requests_real_tr_id_and_reference_time(settings):
    from datetime import datetime

    real_settings = replace(settings, paper=False)

    def responder(request):
        assert request.url.path == "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"
        assert request.headers["tr_id"] == "FHKST03010230"
        assert request.url.params["FID_INPUT_ISCD"] == "005930"
        assert request.url.params["FID_INPUT_DATE_1"] == "20241108"
        assert request.url.params["FID_INPUT_HOUR_1"] == "140000"
        assert request.url.params["FID_PW_DATA_INCU_YN"] == "Y"
        return httpx.Response(
            200,
            json={
                "rt_cd": "0",
                "output1": {"hts_kor_isnm": "삼성전자"},
                "output2": [
                    {
                        "stck_bsop_date": "20241108",
                        "stck_cntg_hour": "140000",
                        "stck_prpr": "57300",
                    },
                    {},  # KIS는 빈 dict로 패딩할 수 있다
                ],
            },
        )

    with make_client(real_settings, responder) as client:
        rows = client.get_intraday_candles("005930", datetime(2024, 11, 8, 14, 0, 0))
    assert len(rows) == 1
    assert rows[0]["stck_prpr"] == "57300"


def test_get_intraday_candles_rejects_paper_trading_before_any_request(settings):
    from datetime import datetime

    def responder(request):  # pragma: no cover - must never be reached
        raise AssertionError("paper trading must not call KIS for this endpoint")

    with make_client(settings, responder) as client:
        with pytest.raises(KisError, match="모의투자"):
            client.get_intraday_candles("005930", datetime(2024, 11, 8, 14, 0, 0))


def test_get_market_holidays_requests_real_tr_id_and_reference_date(settings):
    from datetime import date

    real_settings = replace(settings, paper=False)

    def responder(request):
        assert request.url.path == "/uapi/domestic-stock/v1/quotations/chk-holiday"
        assert request.headers["tr_id"] == "CTCA0903R"
        assert request.url.params["BASS_DT"] == "20221227"
        return httpx.Response(
            200,
            json={
                "rt_cd": "0",
                "output": [
                    {
                        "bass_dt": "20221227",
                        "wday_dvsn_cd": "03",
                        "bzdy_yn": "Y",
                        "tr_day_yn": "Y",
                        "opnd_yn": "Y",
                        "sttl_day_yn": "Y",
                    }
                ],
            },
        )

    with make_client(real_settings, responder) as client:
        rows = client.get_market_holidays(date(2022, 12, 27))
    assert len(rows) == 1
    assert rows[0]["opnd_yn"] == "Y"


def test_get_market_holidays_rejects_paper_trading_before_any_request(settings):
    from datetime import date

    def responder(request):  # pragma: no cover - must never be reached
        raise AssertionError("paper trading must not call KIS for this endpoint")

    with make_client(settings, responder) as client:
        with pytest.raises(KisError, match="모의투자"):
            client.get_market_holidays(date(2022, 12, 27))


def test_market_buy_order_uses_paper_tr_id(settings):
    def responder(request):
        assert request.url.path == "/uapi/domestic-stock/v1/trading/order-cash"
        assert request.headers["tr_id"] == "VTTC0012U"  # paper buy
        body = json.loads(request.content)
        assert body["PDNO"] == "005930"
        assert body["ORD_DVSN"] == "01"  # market order
        assert body["ORD_QTY"] == "10"
        assert body["ORD_UNPR"] == "0"
        return httpx.Response(200, json={"rt_cd": "0", "output": {"ODNO": "0000117057"}})

    with make_client(settings, responder) as client:
        result = client.order("buy", "005930", 10)
    assert result["ODNO"] == "0000117057"


def test_limit_sell_order(settings):
    def responder(request):
        assert request.headers["tr_id"] == "VTTC0011U"  # paper sell
        body = json.loads(request.content)
        assert body["ORD_DVSN"] == "00"  # limit order
        assert body["ORD_UNPR"] == "70000"
        return httpx.Response(200, json={"rt_cd": "0", "output": {"ODNO": "1"}})

    with make_client(settings, responder) as client:
        client.order("sell", "005930", 5, price=70000)


def test_get_daily_executions_queries_by_date_range(settings):
    from datetime import date

    def responder(request):
        assert request.url.path == "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
        assert request.headers["tr_id"] == "VTTC0081R"  # paper, 3개월 이내
        assert request.url.params["INQR_STRT_DT"] == "20260301"
        assert request.url.params["INQR_END_DT"] == "20260310"
        assert request.url.params["ODNO"] == ""
        return httpx.Response(
            200,
            json={
                "rt_cd": "0",
                "output1": [{"odno": "0000117057", "tot_ccld_qty": "10", "avg_prvs": "71000"}],
                "output2": {},
            },
        )

    with make_client(settings, responder) as client:
        rows = client.get_daily_executions(date(2026, 3, 1), date(2026, 3, 10))
    assert rows == [{"odno": "0000117057", "tot_ccld_qty": "10", "avg_prvs": "71000"}]


def test_get_daily_executions_filters_by_order_number(settings):
    from datetime import date

    def responder(request):
        assert request.url.params["ODNO"] == "0000117057"
        assert request.url.params["PDNO"] == "005930"
        return httpx.Response(200, json={"rt_cd": "0", "output1": [], "output2": {}})

    with make_client(settings, responder) as client:
        client.get_daily_executions(
            date(2026, 3, 1),
            date(2026, 3, 10),
            symbol="005930",
            broker_order_no="0000117057",
        )


def test_api_level_failure_raises_kis_error(settings):
    def responder(request):
        return httpx.Response(
            200, json={"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "기간이 만료된 token 입니다."}
        )

    with make_client(settings, responder) as client:
        with pytest.raises(KisError, match="EGW00123"):
            client.get_quote("005930")


def test_invalid_side_is_rejected_before_any_request(settings):
    def responder(request):  # pragma: no cover - must never be reached
        raise AssertionError("no HTTP request expected")

    with make_client(settings, responder) as client:
        with pytest.raises(ValueError, match="side"):
            client.order("short", "005930", 1)


def test_kis_error_body_survives_http_500(settings):
    """KIS는 자신의 진단(EGW02007 등)을 HTTP 500 본문에 담아 보낸다.

    실제로 모의투자 도메인에 실전 앱키로 잔고를 조회했을 때 이 응답을
    받았고, 당시에는 raise_for_status()가 먼저 터지면서 원인 메시지 대신
    계좌번호가 박힌 URL만 남았다.
    """

    def responder(request):
        return httpx.Response(
            500,
            json={
                "rt_cd": "1",
                "msg_cd": "EGW02007",
                "msg1": "해당 앱키는 모의투자용 앱키가 아닙니다.",
            },
        )

    with make_client(settings, responder) as client:
        with pytest.raises(KisError, match="EGW02007: 해당 앱키는 모의투자용 앱키가 아닙니다."):
            client.get_balance()


def test_non_kis_http_error_still_raises_http_status_error(settings):
    """rt_cd가 없는 응답은 KIS가 만든 것이 아니다 — 판단하지 않고 넘긴다."""

    def responder(request):
        return httpx.Response(502, text="<html>gateway timeout</html>")

    with make_client(settings, responder) as client:
        with pytest.raises(httpx.HTTPStatusError):
            client.get_quote("005930")
