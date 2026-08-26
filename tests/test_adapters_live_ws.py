"""adapters/live_ws.py 테스트.

실제 KIS 서버 대신 websockets.serve()로 로컬 가짜 서버(FakeKisServer)를
띄운다 — 네트워크 밖으로 나가지 않고, LiveTickStream과 완전히 대칭인
스레드+이벤트루프 구조라 실전과 같은 방식으로 검증한다.
"""

import asyncio
import json
import logging
import queue
import threading

import websockets

from sontrader.adapters.live_ws import LiveTickStream

_RECORD = (
    "005930^093354^71900^5^-100^-0.14^72023.83^72100^72400^71700^71900^71800^1^"
    "3052507^219853241700^5105^6937^1832^84.90^1366314^1159996^1^0.39^20.28^"
    "090020^5^-200^090820^5^-500^092619^2^200^20230612^20^N^65945^216924^"
    "1118750^2199206^0.05^2424142^125.92^0^^72100"
)


def _tick_message(symbol: str, hhmmss: str, price: int, volume: int = 1) -> str:
    fields = _RECORD.split("^")
    fields[0] = symbol
    fields[1] = hhmmss
    fields[2] = str(price)
    fields[12] = str(volume)
    return "0|H0STCNT0|001|" + "^".join(fields)


class FakeKisServer:
    """연결마다 미리 정해둔 메시지를 순서대로 보내는 로컬 웹소켓 서버."""

    def __init__(self) -> None:
        self.received: queue.Queue[str] = queue.Queue()
        self.connection_count = 0
        self.port: int | None = None
        self._programmed: list[tuple[list[str], bool]] = []
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._ready = threading.Event()

    def program(self, messages: list[str], *, close_after: bool = False) -> None:
        """다음 연결에서 보낼 메시지와, 다 보낸 뒤 연결을 끊을지를 예약한다."""
        self._programmed.append((messages, close_after))

    @property
    def url(self) -> str:
        return f"ws://localhost:{self.port}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)

    def stop(self) -> None:
        if self._loop is not None and self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        if self._thread is not None:
            self._thread.join(5.0)

    def _run(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        async with websockets.serve(self._handler, "localhost", 0) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            await self._stop_event.wait()

    async def _handler(self, ws) -> None:
        idx = self.connection_count
        self.connection_count += 1
        messages, close_after = (
            self._programmed[idx] if idx < len(self._programmed) else ([], False)
        )
        for msg in messages:
            await ws.send(msg)
        if close_after:
            await ws.close()
            return
        async for msg in ws:
            self.received.put(msg)


def test_subscribes_to_each_symbol_on_connect():
    server = FakeKisServer()
    server.start()
    try:
        stream = LiveTickStream(
            server.url, "approval-key-1", ["005930", "000660"], lambda bar: None
        )
        stream.start()
        try:
            first = json.loads(server.received.get(timeout=2.0))
            second = json.loads(server.received.get(timeout=2.0))
        finally:
            stream.stop()

        assert first["header"]["approval_key"] == "approval-key-1"
        assert first["body"]["input"]["tr_id"] == "H0STCNT0"
        assert {first["body"]["input"]["tr_key"], second["body"]["input"]["tr_key"]} == {
            "005930",
            "000660",
        }
    finally:
        server.stop()


def test_tick_messages_are_aggregated_into_bars_via_on_bar():
    server = FakeKisServer()
    server.program(
        [
            _tick_message("005930", "093300", 71000, volume=10),
            _tick_message("005930", "093330", 71500, volume=5),
            _tick_message("005930", "093400", 71200, volume=7),  # 다음 분 — 09:33 봉을 확정시킴
        ]
    )
    server.start()
    bars: queue.Queue = queue.Queue()
    try:
        stream = LiveTickStream(server.url, "key", ["005930"], bars.put)
        stream.start()
        try:
            bar = bars.get(timeout=2.0)
        finally:
            stream.stop()

        assert bar.symbol == "005930"
        assert bar.open == 71000
        assert bar.high == 71500
        assert bar.low == 71000
        assert bar.close == 71500
        assert bar.volume == 15
    finally:
        server.stop()


def test_json_control_messages_do_not_crash_the_stream():
    server = FakeKisServer()
    server.program(
        [
            json.dumps(
                {
                    "header": {"tr_id": "H0STCNT0"},
                    "body": {"rt_cd": "0", "msg1": "SUBSCRIBE SUCCESS"},
                }
            ),
            _tick_message("005930", "093300", 71000),
            _tick_message("005930", "093400", 71100),
        ]
    )
    server.start()
    bars: queue.Queue = queue.Queue()
    try:
        stream = LiveTickStream(server.url, "key", ["005930"], bars.put)
        stream.start()
        try:
            bar = bars.get(timeout=2.0)
        finally:
            stream.stop()

        assert bar.close == 71000
    finally:
        server.stop()


def test_reconnects_after_the_server_closes_the_connection():
    server = FakeKisServer()
    server.program([], close_after=True)  # 1번째 연결: 바로 끊는다
    server.program(
        [
            _tick_message("005930", "093300", 71000),
            _tick_message("005930", "093400", 71100),
        ]
    )  # 2번째 연결(재연결): 실제 데이터
    server.start()
    bars: queue.Queue = queue.Queue()
    try:
        stream = LiveTickStream(server.url, "key", ["005930"], bars.put, reconnect_delay=0.2)
        stream.start()
        try:
            bar = bars.get(timeout=5.0)
        finally:
            stream.stop()

        assert bar.close == 71000
        assert server.connection_count == 2
    finally:
        server.stop()


def test_stop_terminates_the_background_thread():
    server = FakeKisServer()
    server.start()
    try:
        stream = LiveTickStream(server.url, "key", ["005930"], lambda bar: None)
        stream.start()
        stream.stop()

        assert not stream._thread.is_alive()
    finally:
        server.stop()


# --- 구독 ACK 검사 (T18) ----------------------------------------------------
#
# 예전에는 JSON 제어 메시지를 통째로 버렸다. 그러면 KIS 실시간 등록 건수
# 한도를 넘겼을 때 초과분이 조용히 안 붙고, 그 종목만 분봉이 비어도 아무도
# 모른다. 여기서 무게를 두는 것은 **거부가 드러나는가**와 **제어 메시지
# 하나로 수신 루프가 죽지 않는가** 둘이다.


def make_stream() -> LiveTickStream:
    """소켓 없이 `_handle_message`만 직접 두드리기 위한 인스턴스."""
    return LiveTickStream("ws://unused", "key", ["005930", "000660"], lambda _bar: None)


def ack(symbol: str, *, rt_cd: str, msg_cd: str = "OPSP0000", msg1: str = "") -> str:
    return json.dumps(
        {
            "header": {"tr_id": "H0STCNT0", "tr_key": symbol},
            "body": {"rt_cd": rt_cd, "msg_cd": msg_cd, "msg1": msg1},
        }
    )


def test_a_successful_ack_marks_the_symbol_subscribed():
    stream = make_stream()

    stream._handle_message(ack("005930", rt_cd="0", msg1="SUBSCRIBE SUCCESS"))

    assert stream.subscribed_symbols == frozenset({"005930"})
    assert stream.rejected_symbols == frozenset()


def test_a_rejected_ack_is_surfaced_not_swallowed(caplog):
    """거부가 로그에 ERROR로 남아야 한다 — 이게 T18의 핵심이다."""
    stream = make_stream()

    with caplog.at_level(logging.ERROR):
        stream._handle_message(ack("000660", rt_cd="1", msg1="OVER SUBSCRIBE LIMIT"))

    assert stream.rejected_symbols == frozenset({"000660"})
    assert stream.subscribed_symbols == frozenset()
    assert "실시간 등록 거부" in caplog.text
    assert "000660" in caplog.text
    assert "OVER SUBSCRIBE LIMIT" in caplog.text


def test_partial_subscription_is_visible_by_comparing_counts():
    """요청 2종목 중 1종목만 붙은 상태가 조회로 드러나야 한다."""
    stream = make_stream()

    stream._handle_message(ack("005930", rt_cd="0"))
    stream._handle_message(ack("000660", rt_cd="1", msg1="LIMIT"))

    assert len(stream.subscribed_symbols) == 1
    assert set(stream._symbols) - stream.subscribed_symbols == {"000660"}


def test_pingpong_is_not_treated_as_a_subscription_result():
    """keepalive는 정상 메시지다 — 매번 로그를 남기면 잡음만 된다."""
    stream = make_stream()

    stream._handle_message(json.dumps({"header": {"tr_id": "PINGPONG", "datetime": "20260826"}}))

    assert stream.subscribed_symbols == frozenset()
    assert stream.rejected_symbols == frozenset()


def test_malformed_control_message_does_not_raise(caplog):
    """제어 메시지 하나 때문에 수신 루프가 끊기면 스트림 전체가 재연결로 들어간다."""
    stream = make_stream()

    with caplog.at_level(logging.WARNING):
        stream._handle_message("{not valid json")

    assert "해석하지 못했다" in caplog.text


def test_control_message_without_a_status_code_is_ignored():
    stream = make_stream()

    stream._handle_message(json.dumps({"header": {"tr_id": "H0STCNT0"}, "body": {"output": {}}}))

    assert stream.subscribed_symbols == frozenset()
    assert stream.rejected_symbols == frozenset()


def test_a_later_success_clears_an_earlier_rejection():
    """재연결 후 재구독이 성공하면 거부 상태가 남아 있으면 안 된다."""
    stream = make_stream()

    stream._handle_message(ack("005930", rt_cd="1"))
    stream._handle_message(ack("005930", rt_cd="0"))

    assert stream.subscribed_symbols == frozenset({"005930"})
    assert stream.rejected_symbols == frozenset()
