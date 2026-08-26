"""KIS 웹소켓 실시간체결가 연결 (실시간-003, H0STCNT0).

01문서 §2.2 "웹소켓 분봉(지원 시)"을 채운다. `adapters/live_ticks.py`(순수
파싱·집계)를 실제 소켓에 연결하는 부분 — 여기부터는 부작용(네트워크,
스레드)이 있다.

## 왜 이 클래스만 asyncio를 쓰는가

프로젝트 나머지는 전부 동기 코드다(`KisClient`는 `httpx.Client`, `run_cycle`은
평범한 함수). 전체를 비동기로 바꾸는 비용(모든 계층·테스트 405개 이상을
다시 씀)에 비해 이 시스템 규모(계좌 1개, 워치리스트 ≤50종목)에서 얻는
이득이 없다고 판단해 이 부분만 격리한다. 실제로 필요한 건 "웹소켓
수신이 텔레그램 폴링·매매 사이클을 막지 않는 것"뿐이고, 이건 별도
스레드에서 자체 이벤트 루프를 돌리는 것만으로 충분하다 — asyncio를 쓰는
이유는 (스레드가 아니라) 이 클래스 안에서 "소켓 수신을 기다리면서 동시에
정지 신호도 기다리는" 두 가지 대기를 자연스럽게 표현하기 위해서다.

## 재연결

`_main()`은 죽지 않는다 — 연결이 끊기면(서버가 닫거나 네트워크 오류)
`_reconnect_delay`초 뒤 다시 연결하고 전 종목을 재구독한다. 01문서
§6.4 "장중 루프가 죽으면 손절이 발동하지 않으며, 조용히 죽으면
인지조차 못 한다"와 같은 이유 — 이 스트림이 조용히 멈추면 트레일링
스톱 판정에 쓰일 최신 분봉이 끊긴다.

## 구독 ACK 검사

구독 확인 JSON의 `rt_cd`를 검사해 **거부를 로그로 드러낸다**
(`_handle_control_message`). 예전에는 JSON 제어 메시지를 통째로 버렸는데,
그러면 KIS 실시간 등록 건수 한도를 넘겼을 때 초과분이 조용히 안 붙고
그 종목만 분봉이 비어도 아무도 모른다 (`docs/system/03-운영.md` T18).

**한도 값 자체는 코드에 박지 않는다.** 정확한 값을 확인하지 못했고, 추측한
상수로 미리 자르면 멀쩡한 종목까지 구독하지 않게 된다. 대신 거부를 드러내서
한도가 실측으로 보이게 했다. `subscribed_symbols` / `rejected_symbols`로
호출자가 대조할 수 있다.

## 아직 하지 않는 것

거부를 **알림(텔레그램)으로 연결하지 않는다.** 이 스트림을 `apps/live.py`에
실제로 연결하는 슬라이스에서 다룬다 — 지금은 로그까지다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable, Sequence

import websockets

from sontrader.adapters.live_ticks import MinuteBarAggregator, parse_tick_message
from sontrader.core.types import Bar

log = logging.getLogger(__name__)

TR_ID = "H0STCNT0"
_TR_TYPE_SUBSCRIBE = "1"
_PINGPONG_TR_ID = "PINGPONG"  # 서버 keepalive — 상태 코드가 없는 정상 메시지
_RT_CD_OK = "0"


def _subscribe_payload(approval_key: str, symbol: str, *, custtype: str) -> str:
    return json.dumps(
        {
            "header": {
                "approval_key": approval_key,
                "custtype": custtype,
                "tr_type": _TR_TYPE_SUBSCRIBE,
                "content-type": "utf-8",
            },
            "body": {"input": {"tr_id": TR_ID, "tr_key": symbol}},
        }
    )


class LiveTickStream:
    """`symbols`를 구독해 완성된 1분봉을 `on_bar` 콜백으로 흘려보낸다.

    별도 스레드에서 자체 이벤트 루프를 돌며 동작한다. `on_bar`는 그
    백그라운드 스레드에서 호출되므로, 스레드 세이프하지 않은 자원(DB
    커넥션 등)을 콜백 안에서 직접 쓰면 안 된다 — 큐 등으로 넘겨야 한다.
    """

    def __init__(
        self,
        ws_url: str,
        approval_key: str,
        symbols: Sequence[str],
        on_bar: Callable[[Bar], None],
        *,
        custtype: str = "P",
        reconnect_delay: float = 5.0,
    ) -> None:
        self._ws_url = ws_url
        self._approval_key = approval_key
        self._symbols = list(symbols)
        self._on_bar = on_bar
        self._custtype = custtype
        self._reconnect_delay = reconnect_delay
        self._aggregator = MinuteBarAggregator()

        # 구독 등록 결과. 요청 수와 성공 수가 다르면 일부만 붙은 것이다 —
        # 그 종목은 틱이 안 와 분봉이 안 쌓이는데, ACK를 버리면 아무도 모른다.
        self._subscribed: set[str] = set()
        self._rejected: set[str] = set()

        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("already started")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)

    def stop(self, timeout: float | None = 5.0) -> None:
        if self._loop is not None and self._task is not None:
            self._loop.call_soon_threadsafe(self._task.cancel)
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        self._ready.set()
        failures = 0
        try:
            while True:
                try:
                    await self._connect_and_listen()
                    failures = 0
                except Exception as exc:  # noqa: BLE001 — 어떤 오류든 재연결한다
                    # 조용히 삼키면 스트림이 죽은 것을 아무도 모른다(01문서 §6.4
                    # "조용히 죽으면 인지조차 못 한다"). 첫 실패는 흔한 끊김이라
                    # WARN, 반복되면 사람이 봐야 하므로 ERROR로 올린다.
                    failures += 1
                    level = logging.WARNING if failures == 1 else logging.ERROR
                    log.log(
                        level,
                        "웹소켓 연결 끊김 (%d회 연속) — %.0f초 뒤 재연결: %s",
                        failures,
                        self._reconnect_delay,
                        exc,
                    )
                await asyncio.sleep(self._reconnect_delay)
        except asyncio.CancelledError:
            pass

    async def _connect_and_listen(self) -> None:
        async with websockets.connect(self._ws_url) as ws:
            for symbol in self._symbols:
                await ws.send(
                    _subscribe_payload(self._approval_key, symbol, custtype=self._custtype)
                )
            async for raw in ws:
                self._handle_message(raw)

    @property
    def subscribed_symbols(self) -> frozenset[str]:
        """등록 성공을 확인한 종목. 요청한 `symbols`와 비교하면 누락이 보인다."""
        return frozenset(self._subscribed)

    @property
    def rejected_symbols(self) -> frozenset[str]:
        """등록이 거부된 종목 — 한도 초과가 여기로 드러난다."""
        return frozenset(self._rejected)

    def _handle_message(self, raw: str) -> None:
        if raw.startswith("{"):
            self._handle_control_message(raw)
            return
        for tick in parse_tick_message(raw):
            bar = self._aggregator.add(tick)
            if bar is not None:
                self._on_bar(bar)

    def _handle_control_message(self, raw: str) -> None:
        """구독 ACK 등 JSON 제어 메시지.

        **예전에는 통째로 버렸다.** 그러면 등록 거부가 조용히 사라진다 —
        KIS 실시간 등록 건수 한도를 넘기면 초과분이 거부되는데, 거부 통보가
        바로 이 형식으로 온다. 버리면 그 종목들은 틱이 안 와 분봉이 안 쌓이는데
        로그에는 아무것도 남지 않는다 (`docs/system/03-운영.md` T18).

        **한도 값을 코드에 박지 않는다.** 정확한 값을 확인하지 못했고, 추측한
        상수로 미리 자르면 맞을 때보다 틀릴 때의 피해가 크다(멀쩡한 종목을
        구독 안 함). 대신 **거부를 있는 그대로 드러내서** 한도가 실측으로
        보이게 한다.

        해석 실패·예상 밖 모양에도 예외를 던지지 않는다 — 제어 메시지 하나
        때문에 수신 루프가 끊기면 스트림 전체가 재연결로 들어간다.
        """
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("웹소켓 제어 메시지를 해석하지 못했다: %.200s", raw)
            return
        if not isinstance(payload, dict):
            return

        header = payload.get("header")
        header = header if isinstance(header, dict) else {}
        if header.get("tr_id") == _PINGPONG_TR_ID:
            return  # 정상 keepalive — 매번 로그를 남기면 잡음만 된다

        body = payload.get("body")
        if not isinstance(body, dict) or "rt_cd" not in body:
            return  # 상태 코드가 없는 제어 메시지는 판정할 것이 없다

        symbol = header.get("tr_key") or "?"
        if body.get("rt_cd") == _RT_CD_OK:
            self._subscribed.add(symbol)
            self._rejected.discard(symbol)
            log.debug(
                "실시간 등록 성공 %s (%d/%d)", symbol, len(self._subscribed), len(self._symbols)
            )
            return

        # 거부. 조용히 넘어가면 그 종목만 분봉이 비고 아무도 모른다.
        self._rejected.add(symbol)
        log.error(
            "실시간 등록 거부 %s — rt_cd=%s msg_cd=%s %s "
            "(요청 %d종목 중 성공 %d, 거부 %d; 등록 건수 한도 초과일 수 있다)",
            symbol,
            body.get("rt_cd"),
            body.get("msg_cd"),
            body.get("msg1", ""),
            len(self._symbols),
            len(self._subscribed),
            len(self._rejected),
        )
