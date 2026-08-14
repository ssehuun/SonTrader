"""KIS 웹소켓 실시간체결가(H0STCNT0) → 1분봉 순수 변환 로직.

01문서 §2.2 "엔진은 1분봉만 소비한다"를 채우는 실시간 수집원의 앞단이다.
구현 계획에 번호가 매겨진 항목은 아니다 — `apps/live.py`(장중 실행)가
필요로 해서 이번에 추가한다. `data/prices.py`(일봉)와 짝을 이루지만 주기가
다르다.

## 이 파일이 하지 않는 것

웹소켓 연결·구독·재연결은 여기 없다(다음 슬라이스). 여기서는 (1) KIS가
보내는 원시 텍스트를 구조화된 `Tick`으로 바꾸고, (2) 틱을 심볼별로 1분
단위로 묶어 완성된 `core.types.Bar`를 내보내는 것만 한다 — 네트워크도
시각도 모르는 순수 로직이라 실제 소켓 없이도 문서의 예제 메시지로 바로
검증할 수 있다.

## 메시지 형식 (실시간-003 문서 개요, 예제로 검증)

``{암호화여부}|H0STCNT0|{건수}|{필드46개}^{필드46개}^...`` — 건수만큼 46개
필드 그룹이 이어 붙는다. 필요한 값(종목코드·체결시각·현재가·체결거래량·
영업일자)만 골라 쓰고 나머지 41개(호가·거래대금 등)는 버린다.

암호화된 메시지(첫 필드가 "0"이 아님)는 다루지 않는다 — AES256 복호화를
구현하지 않았으므로, 조용히 잘못된 값을 만드는 대신 예외를 던진다
(fail-closed). 문서의 응답 예제는 암호화 없음("0")이었다.

## 왜 진행 중인 분(minute)을 절대 내보내지 않는가

`engine/context.py`의 `BarView`가 미래 봉을 구조적으로 차단하는 것과 같은
이유다 — 미완성 봉을 완성 봉처럼 노출하면 트레일링 스톱 계산이 그 분이
끝나기도 전의 값으로 흔들린다. `MinuteBarAggregator.add()`는 분이 바뀌는
틱이 들어와야만 그 **직전** 분의 봉을 반환한다. 마지막 분은 분이 바뀌는
다음 틱이 영영 오지 않을 수 있으므로(장 마감, 구독 해제) `flush()`로
강제 확정하는 경로를 별도로 둔다.

## 틱은 심볼별로 시간순 도착을 가정한다

같은 심볼의 틱이 시간 역순으로 들어오면(네트워크 지연·재전송 등)
`MinuteBarAggregator.add()`가 예외를 던진다 — 조용히 받아들이면 이미
확정해 내보낸 봉이 사실은 틀렸다는 뜻이 되므로, 어느 봉도 믿을 수 없게
되는 것보다는 여기서 드러나는 편이 낫다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sontrader.core.types import Bar

_FIELDS_PER_RECORD = 46
_IDX_SYMBOL = 0
_IDX_TIME = 1
_IDX_PRICE = 2
_IDX_VOLUME = 12  # CNTG_VOL — 이번 체결 건의 거래량 (누적이 아니다)
_IDX_DATE = 33  # BSOP_DATE


@dataclass(frozen=True)
class Tick:
    symbol: str
    ts: datetime
    price: int
    volume: int


def parse_tick_message(raw: str) -> list[Tick]:
    """H0STCNT0 실시간체결가 원시 메시지 하나(여러 건 포함 가능)를 파싱한다."""
    encrypted_flag, tr_id, count_str, blob = raw.split("|", 3)
    if encrypted_flag != "0":
        raise NotImplementedError(
            f"encrypted real-time message (flag={encrypted_flag!r}) — AES256 복호화 미구현"
        )
    if tr_id != "H0STCNT0":
        raise ValueError(f"unexpected tr_id: {tr_id!r}")

    count = int(count_str)
    fields = blob.split("^")
    expected = count * _FIELDS_PER_RECORD
    if len(fields) != expected:
        raise ValueError(f"expected {expected} fields for {count} record(s), got {len(fields)}")

    ticks = []
    for i in range(count):
        record = fields[i * _FIELDS_PER_RECORD : (i + 1) * _FIELDS_PER_RECORD]
        ticks.append(
            Tick(
                symbol=record[_IDX_SYMBOL],
                ts=datetime.strptime(record[_IDX_DATE] + record[_IDX_TIME], "%Y%m%d%H%M%S"),
                price=int(record[_IDX_PRICE]),
                volume=int(record[_IDX_VOLUME]),
            )
        )
    return ticks


@dataclass
class _PartialBar:
    minute: datetime
    open: int
    high: int
    low: int
    close: int
    volume: int

    def extend(self, tick: Tick) -> None:
        self.high = max(self.high, tick.price)
        self.low = min(self.low, tick.price)
        self.close = tick.price
        self.volume += tick.volume

    def to_bar(self, symbol: str) -> Bar:
        return Bar(
            symbol=symbol,
            ts=self.minute,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
        )


class MinuteBarAggregator:
    """틱을 심볼별로 1분 단위로 묶어 완성된 봉만 내보낸다."""

    def __init__(self) -> None:
        self._partial: dict[str, _PartialBar] = {}

    def add(self, tick: Tick) -> Bar | None:
        """틱 하나를 반영한다. 분이 바뀌는 틱이면 직전 분의 완성 봉을,
        아니면 None을 반환한다."""
        minute = tick.ts.replace(second=0, microsecond=0)
        partial = self._partial.get(tick.symbol)

        if partial is None:
            self._start(tick, minute)
            return None

        if minute < partial.minute:
            raise ValueError(
                f"tick for {tick.symbol!r} at {tick.ts} is older than "
                f"in-progress minute {partial.minute} — ticks must arrive in order"
            )
        if minute == partial.minute:
            partial.extend(tick)
            return None

        completed = partial.to_bar(tick.symbol)
        self._start(tick, minute)
        return completed

    def flush(self, symbol: str) -> Bar | None:
        """진행 중인 분을 강제로 확정한다. 장 마감·구독 해제 시 호출하지
        않으면 그 심볼의 마지막 분이 영영 나오지 않는다."""
        partial = self._partial.pop(symbol, None)
        return partial.to_bar(symbol) if partial is not None else None

    def _start(self, tick: Tick, minute: datetime) -> None:
        self._partial[tick.symbol] = _PartialBar(
            minute=minute,
            open=tick.price,
            high=tick.price,
            low=tick.price,
            close=tick.price,
            volume=tick.volume,
        )
