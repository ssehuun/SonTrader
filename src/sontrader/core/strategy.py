"""전략: 목표 포트폴리오 산출 (구현 계획 4단계). 순수 함수.

02 문서 §3.3의 ``build_target(ctx) -> Target``. 동작 순서:

1. 보유 종목마다 `exit_rules.evaluate()` → 발동하면 목표에서 weight=0, urgency=IMMEDIATE
2. `ctx.new_events` 중 워치리스트 종목만, 그리고 아직 보유하지 않은 종목만 후보로 남긴다
3. 후보 이벤트를 `ctx.judgments`에서 조회해 진입 여부(verdict)와 청산조건을 확정한다.
   **LLM은 여기서 호출하지 않는다** — `judgments`는 엔진이 미리 채워 넣은 조회 결과다
   (구조 원칙 1: core는 부작용이 없다. `llm/judge.py`는 6단계 미착수 항목).
4. 슬롯 상한(5종목)은 여기서 강제하지 않는다 — `core/gate.py`의 몫이다(설계 2.5절).
   여기서는 신호가 경합할 때 어느 쪽이 이길지 순서만 정해서 넘긴다.

## 진입 트리거가 둘인 이유 (`EntryTrigger`)

위 2~3번은 `EntryTrigger.EVENT`일 때의 경로다. `WATCHLIST_RANK`를 고르면
이벤트도 LLM도 보지 않고 워치리스트 순위대로 슬롯을 채운다. 청산·게이트·
집행은 완전히 동일하다.

두 번째 경로가 필요한 이유는 두 가지다.

**1. 이벤트 레이어를 평가할 기준선.** "공시가 진입 타이밍을 개선하는가"는
비교 없이 답할 수 없다. 진입 트리거만 다른 두 arm을 같은 데이터로 돌려야
차이가 이벤트 때문인지 알 수 있다.

**2. LLM 없이 엔진 전체를 관통 검증.** EVENT 모드는 `ctx.judgments`가
비면 진입 후보가 0건이라, LLM 키가 없으면 백테스트가 거래를 한 건도
만들지 못한다 — 게이트·diff·체결·청산·비용이 실데이터로 한 번도 안 돌아본
상태가 된다. WATCHLIST_RANK는 API 비용 0으로 그 경로를 열어준다.

## 명시적으로 확정한 것들

**1. 보유 종목의 목표 비중은 "현재 비중"이지 고정 20%가 아니다.**
신규 진입은 `entry_weight`(기본 20%)로 배분하지만, 보유 중에는 그 비중을 다시
강제하지 않는다. 고정 20%를 계속 목표로 두면 가격이 no-trade-band(기본 2%)보다
크게 움직일 때마다 리밸런싱 매매가 발생하는데, 이는 (a) 회전율을 낮게 유지해야
한다는 요건(01문서 §7 "알려진 한계"), (b) ATR 트레일링이 "수익 종목을 계속
태우는" 것을 목적으로 한다는 것과 충돌할 수 있다 — 비중 리밸런싱이 트레일링
스톱보다 먼저 수익 종목을 20%로 깎아버리면 이익이 조기에 잘린다. 그래서 보유
종목은 현재 마크투마켓 비중(`qty × 마지막 종가 / equity`)을 그대로 목표로
돌려주어 diff의 델타가 0에 가깝게 나오게 한다. 가격을 못 구하면(호가 없음)
`entry_weight`로 대체한다 — 이 경우 diff가 어차피 가격이 없으면 매수 주문을
만들지 않으므로 무해하다(청산 판단에는 영향 없음. exit_rules는 별도 봉을 쓴다).

**2. 신규 진입 후보의 우선순위는 확신도(confidence) 내림차순이다.**
게이트가 슬롯 경합을 "먼저 온 순서"로 처리하므로(설계 2.5절, `gate.py`의
"입력 순서를 그대로 존중" 원칙), 그 순서를 전략이 정해야 한다. 확신도가 같으면
종목코드 오름차순으로 깬다 — `core/watchlist.py`와 같은 결정성 규약이다.

**3. 이미 보유 중인 종목에 대한 새 이벤트는 무시한다.**
포트폴리오는 종목 단위이지 이벤트 단위가 아니다(설계 §2.3 "역할 분담: 팩터 =
무엇을 살지, 이벤트 = 언제 살지"). 같은 종목에 이벤트마다 추가 매수를 허용하면
20% 상한과 5슬롯 개념이 무의미해진다. 재진입은 청산 후에만 가능하고, 그마저도
게이트의 쿨다운·동일이벤트 규칙이 막을 수 있다.

**4-b. `max_hold_bars`를 쓰면 `exit_history_bars`가 그보다 커야 한다.**
보유 봉 수는 넘긴 창 안에서만 셀 수 있다. 창이 짧으면 상한이 영영 안 걸리고
조용히 무시되므로 `__post_init__`이 그 조합을 거부한다.

**4. `exit_history_bars`는 봉 주기를 모른 채로 넘기는 개수다.**
`core/exit_rules.py`와 같은 이유로 이 모듈도 봉 주기(1분/1일)를 알지 않는다.
기본값 300은 일봉 기준(ATR 창 + 최대보유일 여유)이며, 분봉을 주입하는 호출자는
이 값을 그에 맞게 늘려야 한다(구조 원칙 1 — 시각·주기는 주입하는 쪽의 책임).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from sontrader.core import exit_rules
from sontrader.core.types import (
    Context,
    ExitReason,
    ExitRule,
    Position,
    Target,
    TargetItem,
    Urgency,
)


class EntryTrigger(Enum):
    """무엇이 신규 진입을 촉발하는가.

    두 값을 두는 목적은 **비교 가능성**이다. 이벤트 트리거가 성과에 기여하는지
    는 기준선 없이 판정할 수 없다 — 같은 유니버스·같은 청산 규칙 위에서 진입
    트리거만 바꿔 돌려봐야 "공시가 진입 타이밍을 개선하는가"에 답할 수 있다.
    """

    EVENT = "event"
    """공시 + LLM 판단 (설계 §2.3의 기본 동작). 이벤트가 없으면 진입도 없다."""

    WATCHLIST_RANK = "watchlist_rank"
    """워치리스트 순위만 보고 슬롯을 채운다. 이벤트도 LLM도 쓰지 않는다."""


@dataclass(frozen=True)
class StrategyConfig:
    # 신규 진입 목표 비중. 설계 1.2절은 "5종목 균등 20%"였으나 **증거금 규칙에
    # 걸려 성립하지 않는다** — 한 사이클에 넣을 수 있는 총액이 `1/1.30 = 76.9%`로
    # 고정이고 종목 수와 무관하다(`core/diff.py`의 「증거금」 절). 4종목 × 19% =
    # 76%가 그 안에 들어가는 조합이다. `max_positions`와 함께 바꿔야 한다.
    entry_weight: float = 0.19
    exit_history_bars: int = 300  # 청산 판정에 넘길 봉 개수 (ATR 창 + 보유기간 여유)
    entry_trigger: EntryTrigger = EntryTrigger.EVENT
    # WATCHLIST_RANK 모드에서 신규 진입에 붙일 청산 조건. EVENT 모드는 LLM이
    # 종목별로 정하므로 이 값을 쓰지 않는다. 여기 둔 이유는 (a) 배선이 이미
    # 있고(CycleConfig.strategy → build_target), (b) "진입에 어떤 청산 조건을
    # 붙일지"가 전략의 결정이기 때문이다. 파라미터 스윕의 주입 지점이 된다.
    exit_rule: ExitRule = field(default_factory=ExitRule)
    # **목표에서 빠진 보유 종목을 청산하는가.** 기본은 아니오 — 모멘텀
    # 워치리스트는 매일 흔들려서, 켜면 순위가 하루 출렁일 때마다 사고판다.
    #
    # 켜야 하는 전략도 있다. 지수 추세 필터처럼 **"신호가 있는 동안만 보유"**가
    # 규칙 자체인 경우다(`종가 > SMA(N)`이면 보유, 아니면 현금). 그런 규칙은
    # 스톱으로 표현할 수 없다 — 손실이 나서 파는 게 아니라 **살 이유가
    # 없어져서** 판다.
    #
    # `core/diff.py`는 이미 "목표에서 빠졌거나 비중 0인 보유 종목 → 전량
    # 청산"을 하고 있었다. 안 일어난 이유는 `build_target()`이 보유 종목을
    # **무조건 목표에 다시 넣기** 때문이다. 이 손잡이는 그 재추가를 끄는 것이지
    # 새 집행 경로를 만드는 것이 아니다.
    exit_when_dropped: bool = False

    def __post_init__(self) -> None:
        if not 0.0 < self.entry_weight <= 1.0:
            raise ValueError(f"entry_weight must be in (0, 1]: {self.entry_weight}")
        if self.exit_history_bars < 1:
            raise ValueError(f"exit_history_bars must be >= 1: {self.exit_history_bars}")
        # `max_hold_bars`는 이 창 안에서만 셀 수 있다 (`exit_rules.bars_held`).
        # 창이 더 짧으면 상한이 **영영 안 걸리고** 조용히 무시된다 — 분봉
        # 세션이 약 380봉이라 기본값 300으로는 세션 하나도 못 담는다.
        # 여기서 막지 않으면 "왜 max_hold가 안 걸리지"를 나중에 되짚어야 한다.
        if (
            self.exit_rule.max_hold_bars is not None
            and self.exit_rule.max_hold_bars > self.exit_history_bars
        ):
            raise ValueError(
                f"exit_history_bars({self.exit_history_bars}) must be >= "
                f"max_hold_bars({self.exit_rule.max_hold_bars}) — "
                "otherwise the bar-count cap can never be reached"
            )


def build_target(ctx: Context, config: StrategyConfig | None = None) -> Target:
    cfg = config or StrategyConfig()
    items: list[TargetItem] = []
    seen: set[str] = set()

    for pos in ctx.positions:
        items.append(_held_item(pos, ctx, cfg))
        seen.add(pos.symbol)

    if cfg.entry_trigger is EntryTrigger.EVENT:
        candidates = _event_entries(ctx, seen)
    else:
        candidates = _watchlist_entries(ctx, seen, cfg)

    for symbol, event_id, exit_rule in candidates:
        items.append(
            TargetItem(
                symbol=symbol,
                weight=cfg.entry_weight,
                urgency=Urgency.NEXT_OPEN,
                exit_rule=exit_rule,
                event_id=event_id,
            )
        )

    return Target(tuple(items))


def _held_item(pos: Position, ctx: Context, cfg: StrategyConfig) -> TargetItem:
    bars = ctx.bars.history(pos.symbol, cfg.exit_history_bars)
    signal = exit_rules.evaluate(
        pos, bars, now=ctx.now, session_bars_remaining=ctx.session_bars_remaining
    )
    if signal is not None:
        return TargetItem(
            symbol=pos.symbol,
            weight=0.0,
            urgency=Urgency.IMMEDIATE,
            event_id=pos.event_id,
            # 판정한 자리에서 사유를 실어 보낸다 (R16). 사후에 다시 계산하면
            # 그때의 봉으로 재판정하게 되어 원래 사유와 갈릴 수 있다.
            exit_reason=signal.reason,
        )
    # 신호 소멸 청산. **스톱보다 뒤에 본다** — 둘 다 해당하면 더 구체적인
    # 사유(스톱)를 남기는 편이 사유별 분해(T28)에 유용하다.
    if cfg.exit_when_dropped and pos.symbol not in ctx.watchlist:
        return TargetItem(
            symbol=pos.symbol,
            weight=0.0,
            urgency=Urgency.IMMEDIATE,
            event_id=pos.event_id,
            exit_reason=ExitReason.SIGNAL,
        )
    return TargetItem(
        symbol=pos.symbol,
        weight=_current_weight(pos, ctx, cfg),
        urgency=Urgency.NEXT_OPEN,
        exit_rule=pos.exit_rule,
        event_id=pos.event_id,
    )


def _current_weight(pos: Position, ctx: Context, cfg: StrategyConfig) -> float:
    """보유 종목의 목표 비중 = **max(현재 비중, 목표 비중)**.

    현재 비중만 돌려주면 diff의 델타가 0이 되어 **덜 채워진 진입이 영구히 덜
    채워진 채로 남는다.** 증거금 상한 때문에 한 사이클에 목표를 다 못 사는
    경우가 있는데(`core/diff.py`의 「증거금」 절), 그때 잔량을 채울 길이 없다.
    실제로 S0(지수 매수보유)이 체결 1건으로 끝나 자본의 22.5%가 영구히 놀았다
    — CAGR −0.70%p.

    **`max`인 이유**: 위쪽은 그대로 둔다. 오른 종목을 목표 비중으로 깎으면
    ATR 트레일링이 "수익 종목을 계속 태우는" 것과 충돌하고 회전율도 올라간다
    (이 모듈 서두 1번). 아래쪽만 되돌린다.

    **손실 종목을 물타기하지 않나** — 실질적으로 안 한다. no-trade band(기본
    2%)를 넘으려면 비중이 목표보다 2%p 넘게 빠져야 하는데, 그 전에 손절(−5%)이
    먼저 걸린다. 되돌림이 실제로 발동하는 것은 **진입 직후 덜 채워진 경우**뿐이고
    그때는 격차가 커서 밴드를 넘는다.
    """
    if ctx.equity <= 0:
        return cfg.entry_weight
    bar = ctx.bars.latest(pos.symbol)
    if bar is None or bar.close <= 0:
        return cfg.entry_weight
    # 1주치를 더해 올려 잡는다. diff가 `int(equity × weight) // price`로 되돌리는데,
    # 그냥 `qty × close / equity`를 주면 부동소수점 오차로 `int()`가 1을 깎아
    # **델타가 −1이 되고 1주짜리 매도가 나간다.** 고가 종목일수록 1주가 커서
    # no-trade band를 넘어 실제로 주문이 나갔다 — 실측에서 NAVER·SK하이닉스에
    # 8.6년간 12건. 여유를 더하면 되돌린 값이 정확히 `pos.qty`가 된다
    # (한 주가 더 나오지는 않는다 — `(qty+1)×close − 1`을 넘지 않기 때문이다).
    current = min(max((pos.qty * bar.close + bar.close - 1) / ctx.equity, 0.0), 1.0)
    return max(current, cfg.entry_weight)


def _watchlist_entries(
    ctx: Context, exclude: set[str], cfg: StrategyConfig
) -> list[tuple[str, str | None, ExitRule]]:
    """워치리스트 순위 그대로 진입 후보를 만든다 (이벤트·LLM 미사용).

    `ctx.watchlist`는 이미 순위 오름차순이다(`data/universe.py`가 그렇게
    저장한다). 게이트가 입력 순서를 슬롯 우선순위로 존중하므로 여기서
    다시 정렬하지 않는다.

    `event_id`는 None이다 — 촉발한 이벤트가 없다. 게이트의 동일이벤트
    검사는 None을 건너뛰므로(`gate.py`), 청산 후 재진입은 쿨다운
    (`GateConfig.cooldown_days`)이 유일한 제동이다. 이벤트 모드에서
    `used_event_ids`가 하던 역할을 여기서는 쿨다운이 대신한다는 뜻이라,
    이 모드를 쓸 때는 쿨다운을 0보다 크게 잡는 편이 안전하다.

    `exit_rule`은 종목과 무관하게 `cfg.exit_rule` 하나로 통일한다 — LLM이
    종목별로 정해주던 최대보유일·스톱을 쓸 수 없기 때문이다. 이게 오히려
    비교에는 유리하다: 두 arm의 차이가 진입 트리거 하나로 좁혀진다.
    """
    return [(symbol, None, cfg.exit_rule) for symbol in ctx.watchlist if symbol not in exclude]


def _event_entries(ctx: Context, exclude: set[str]) -> list[tuple[str, str, ExitRule]]:
    """워치리스트 신규 진입 후보를 확신도 내림차순으로 정렬해 (symbol, event_id, exit_rule)로 반환.

    한 종목에 이벤트가 여러 건이면 확신도가 가장 높은 것만 남긴다 — `Target`은
    종목 중복을 허용하지 않는다.
    """
    watchlist = set(ctx.watchlist)
    best: dict[str, tuple[float, str, ExitRule]] = {}
    for event in ctx.new_events:
        symbol = event.symbol
        if symbol is None or symbol not in watchlist or symbol in exclude:
            continue
        judgment = ctx.judgments.get(event.event_id)
        if judgment is None or not judgment.verdict:
            continue
        current = best.get(symbol)
        if current is None or judgment.confidence > current[0]:
            best[symbol] = (judgment.confidence, event.event_id, judgment.exit_rule)

    ranked = sorted(best.items(), key=lambda kv: (-kv[1][0], kv[0]))
    return [(symbol, event_id, exit_rule) for symbol, (_, event_id, exit_rule) in ranked]
