import json

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


def test_market_buy_order_uses_paper_tr_id(settings):
    def responder(request):
        assert request.url.path == "/uapi/domestic-stock/v1/trading/order-cash"
        assert request.headers["tr_id"] == "VTTC0802U"  # paper buy
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
        assert request.headers["tr_id"] == "VTTC0801U"  # paper sell
        body = json.loads(request.content)
        assert body["ORD_DVSN"] == "00"  # limit order
        assert body["ORD_UNPR"] == "70000"
        return httpx.Response(200, json={"rt_cd": "0", "output": {"ODNO": "1"}})

    with make_client(settings, responder) as client:
        client.order("sell", "005930", 5, price=70000)


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
