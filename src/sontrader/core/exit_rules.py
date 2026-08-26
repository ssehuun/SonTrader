"""청산 규칙 (구현 계획 4단계). 순수 함수 — 시각·데이터는 인자로 주입된다.

설계 4절의 3구간 스톱:

| 구간 | 스톱 레벨 |
|---|---|
| 진입 ~ breakeven_trigger 미만 | 진입가 × (1 + stop_loss_pct) — 기본 −5% 고정 |
| breakeven_trigger 도달 시점 | 진입가 (본전 이동) — 기본 +5% |
| 이후 | max(진입가, high_water − k × ATR) |

기준가는 **진입 체결 가중평균가**이고, 스톱 레벨은 **절대 하향되지 않는다.**

## 명시적으로 확정한 3가지

**1. high_water는 봉 종가의 최대값이다 (고가가 아니라).**
설계 4절은 "최고가"라고 쓰면서 동시에 "트레일링 판정은 분봉 종가 기준, 틱
저가로 판정하지 않는다(장중 스파이크 노이즈 회피)"고 못박는다. 위쪽 꼬리로
high_water를 올리면 스톱이 그만큼 타이트해져 같은 노이즈가 반대 방향으로
들어온다. **이 규칙은 `stop_basis`와 무관하게 유지된다** — 아래를 저가로
판정하더라도 위를 고가로 올리지는 않는다.

**1-b. 이탈 판정 가격은 주입된다 (`ExitRule.stop_basis`).**
설계의 "종가 기준"은 **분봉** 종가를 전제한 문장인데 우리는 일봉으로
판정한다. 분봉 종가는 사실상 연속 감시지만 일봉 종가는 하루 한 번이라,
같은 문구가 전혀 다른 규칙이 된다 — 장중에 스톱을 −15% 깨고 종가에 −4%로
회복한 날을 통째로 놓친다. 어느 쪽이 맞는지는 백테스트로 정할 값이므로
(설계 8절) 기본값만 두고 선택지를 연다. `StopBasis` 독스트링 참고.

**2. 하향 금지는 `max`만으로 보장되지 않는다. 래칫이 필요하다.**
02 문서 §3.4는 "스톱이 하향되지 않는 성질이 max로 자연히 보장된다"고 하지만,
그건 ATR이 상수일 때만 참이다. 변동성이 확대되면 `high_water − k×ATR`은
줄어들고, `max(진입가, …)`는 진입가까지만 막아줄 뿐 그 위에서의 하락은 막지
못한다 (예: 진입가×1.10 → 진입가×1.02). 그래서 `trailing_stop()`은 진입 이후
모든 봉에서 계산한 스톱의 **누적 최대값**을 돌려준다.
이 값도 여전히 봉에서만 재계산되므로 `positions`에 high_water를 저장하지 않는
설계(6.5절)는 그대로 유지된다.

**3. ATR은 True Range의 단순평균이다 (와일더 스무딩이 아니라).**
와일더 EMA는 시드에 의존해서, 진입 시점부터 잘라낸 봉 창으로 재계산하면 원본과
값이 달라진다. 재계산으로 상태를 복원한다는 전제(설계 6.5절)와 정면으로
충돌한다. 단순평균은 마지막 period+1개 봉만으로 완전히 결정된다.

봉의 주기(1분/1일)는 이 모듈이 알지 않는다. `atr_period`는 개수일 뿐이고,
어떤 봉을 넣을지는 주입하는 쪽이 정한다 (설계 8절 — 백테스트로 확정).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from sontrader.core.types import Bar, ExitRule, Position, StopBasis, TechnicalExit

# 본전 이동 문턱은 `ExitRule.breakeven_trigger`로 옮겼다 — 다른 스톱
# 파라미터(stop_loss_pct·atr_k·atr_period)가 전부 거기 있는데 이것만 모듈
# 상수라 주입할 수 없었고, 포지션에 함께 저장되지 않아 전역 값을 바꾸면
# 과거 포지션의 스톱 레벨이 재현되지 않았다.


class ExitReason(str, Enum):
    STOP = "stop"  # 스톱 레벨 이탈 (고정 손절 / 본전 / ATR 트레일링)
    MAX_HOLD = "max_hold"  # 최대 보유기간 상한 — 스톱과 무관하게 별도 강제


@dataclass(frozen=True)
class ExitSignal:
    symbol: str
    reason: ExitReason
    stop_level: float
    trigger_price: int | None  # 판정 근거가 된 가격 (종가 또는 저가; 봉이 없으면 None)


def stop_level(
    entry_price: float,
    high_water: float,
    atr_value: float | None,
    *,
    rule: ExitRule,
) -> float:
    """설계 4절 3구간 스톱의 **한 시점** 값. 시간에 걸친 하향 금지는 여기서 다루지 않는다.

    `atr_value`가 None이면(ATR 창을 채울 봉이 아직 부족) 본전 스톱으로 둔다.
    이 구간에 들어왔다는 것은 이미 `breakeven_trigger` 이상 올랐다는 뜻이므로,
    트레일링을 계산할 수 없다고 해서 고정 손절로 되돌리면 스톱이 하향된다.
    """
    if high_water < entry_price * (1.0 + rule.breakeven_trigger):
        return entry_price * (1.0 + rule.stop_loss_pct)
    if atr_value is None:
        return entry_price
    return max(entry_price, high_water - rule.atr_k * atr_value)


def true_ranges(bars: Sequence[Bar]) -> list[float]:
    """TR 시계열. 첫 봉은 직전 종가가 없어 0으로 둔다 (ATR 창에서 제외된다)."""
    trs = [0.0] * len(bars)
    for i in range(1, len(bars)):
        prev_close = bars[i - 1].close
        bar = bars[i]
        trs[i] = float(
            max(
                bar.high - bar.low,
                abs(bar.high - prev_close),
                abs(bar.low - prev_close),
            )
        )
    return trs


def average_true_range(bars: Sequence[Bar], period: int) -> float | None:
    """마지막 봉 기준 ATR. 봉이 period+1개 미만이면 None (첫 봉의 TR은 못 쓴다)."""
    if period < 1:
        raise ValueError(f"period must be >= 1: {period}")
    series = _atr_series(bars, period)
    return series[-1] if series else None


def trailing_stop(position: Position, bars: Sequence[Bar]) -> float:
    """진입 이후 매 봉에서 계산한 스톱의 **누적 최대값** (하향 금지 래칫).

    `bars`는 시각 오름차순이며, ATR 창을 채우기 위해 **진입 이전 봉을 포함해서**
    넘긴다 (진입 시점부터만 넘기면 초기 구간의 ATR이 비어 본전 스톱으로 대체된다).
    진입 이후 봉이 하나도 없으면 고정 손절 레벨을 돌려준다.
    """
    level, _ = _ratchet(position, bars)
    return level


def evaluate(
    position: Position,
    bars: Sequence[Bar],
    *,
    now: datetime,
) -> ExitSignal | None:
    """청산 발동 여부. 발동하지 않으면 None.

    스톱을 먼저 본다 — 둘 다 해당하면 더 구체적인 사유를 남기는 편이 낫다
    (집행은 어느 쪽이든 시장가 즉시 청산으로 동일하다).
    """
    rule = position.exit_rule
    if rule.technical is not TechnicalExit.ATR_TRAILING:
        # 닫힌 집합에 값이 늘어나면 여기서 분기한다. 판정할 수 없는 규칙을
        # 조용히 통과시키면 스톱이 영영 발동하지 않는다 — fail-closed.
        raise ValueError(f"no evaluator for technical exit rule: {rule.technical!r}")

    for bar in bars:
        if bar.symbol != position.symbol:
            raise ValueError(f"bar symbol {bar.symbol!r} != position symbol {position.symbol!r}")

    level, last = _ratchet(position, bars)
    if last is not None and _stop_price(last, rule.stop_basis) <= level:
        return ExitSignal(
            position.symbol, ExitReason.STOP, level, _stop_price(last, rule.stop_basis)
        )

    # 거래일이 아니라 **달력일**이다 — core는 휴장일 캘린더를 알지 않는다
    # (구조 원칙 1). LLM이 출력하는 "최대보유일"도 달력일 감각에 가깝다.
    held_days = (now.date() - position.entered_at.date()).days
    if held_days >= rule.max_hold_days:
        return ExitSignal(
            position.symbol,
            ExitReason.MAX_HOLD,
            level,
            last.close if last is not None else None,
        )
    return None


def _stop_price(bar: Bar, basis: StopBasis) -> int:
    """스톱 이탈 판정에 쓸 가격.

    `LOW`는 **장중에 스톱을 건드렸으면 이탈**로 본다. 일봉 종가만 보면
    장중에 스톱을 크게 깨고 종가에 일부 회복한 날을 통째로 놓치는데,
    실전에서는 그 사이에 이미 스톱이 발동했을 것이다.

    `high_water`는 이 선택과 무관하게 **항상 종가**다 (모듈 상단 확정사항 1).
    위쪽 꼬리로 high_water를 올리면 스톱이 그만큼 타이트해져, 아래쪽에서
    피하려던 노이즈가 반대 방향으로 그대로 들어온다.
    """
    return bar.low if basis is StopBasis.LOW else bar.close


def _ratchet(position: Position, bars: Sequence[Bar]) -> tuple[float, Bar | None]:
    """(래칫된 스톱 레벨, 진입 이후 마지막 봉)."""
    rule = position.exit_rule
    entry = position.avg_price
    ratchet = entry * (1.0 + rule.stop_loss_pct)

    atr_series = _atr_series(bars, rule.atr_period)
    high_water = entry
    last: Bar | None = None
    for i, bar in enumerate(bars):
        if bar.ts < position.entered_at:
            continue
        last = bar
        high_water = max(high_water, float(bar.close))
        ratchet = max(ratchet, stop_level(entry, high_water, atr_series[i], rule=rule))
    return ratchet, last


def _atr_series(bars: Sequence[Bar], period: int) -> list[float | None]:
    """봉별 ATR(단순평균). 창이 채워지기 전 구간은 None."""
    count = len(bars)
    series: list[float | None] = [None] * count
    if count < period + 1:
        return series

    trs = true_ranges(bars)
    window = sum(trs[1 : period + 1])
    series[period] = window / period
    for i in range(period + 1, count):
        window += trs[i] - trs[i - period]
        series[i] = window / period
    return series
