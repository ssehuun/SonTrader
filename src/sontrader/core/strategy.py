"""전략: 목표 포트폴리오 산출 (구현 계획 4단계). 순수 함수.

02 문서 §3.3의 ``build_target(ctx) -> Target``. 동작 순서:

1. 보유 종목마다 `exit_rules.evaluate()` → 발동하면 목표에서 weight=0, urgency=IMMEDIATE
2. `ctx.new_events` 중 워치리스트 종목만, 그리고 아직 보유하지 않은 종목만 후보로 남긴다
3. 후보 이벤트를 `ctx.judgments`에서 조회해 진입 여부(verdict)와 청산조건을 확정한다.
   **LLM은 여기서 호출하지 않는다** — `judgments`는 엔진이 미리 채워 넣은 조회 결과다
   (구조 원칙 1: core는 부작용이 없다. `llm/judge.py`는 6단계 미착수 항목).
4. 슬롯 상한(5종목)은 여기서 강제하지 않는다 — `core/gate.py`의 몫이다(설계 2.5절).
   여기서는 신호가 경합할 때 어느 쪽이 이길지 순서만 정해서 넘긴다.

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

**4. `exit_history_bars`는 봉 주기를 모른 채로 넘기는 개수다.**
`core/exit_rules.py`와 같은 이유로 이 모듈도 봉 주기(1분/1일)를 알지 않는다.
기본값 300은 일봉 기준(ATR 창 + 최대보유일 여유)이며, 분봉을 주입하는 호출자는
이 값을 그에 맞게 늘려야 한다(구조 원칙 1 — 시각·주기는 주입하는 쪽의 책임).
"""

from __future__ import annotations

from dataclasses import dataclass

from sontrader.core import exit_rules
from sontrader.core.types import Context, ExitRule, Position, Target, TargetItem, Urgency


@dataclass(frozen=True)
class StrategyConfig:
    entry_weight: float = 0.20  # 신규 진입 목표 비중 (설계 1.2절 "5종목 균등 20%")
    exit_history_bars: int = 300  # 청산 판정에 넘길 봉 개수 (ATR 창 + 보유기간 여유)

    def __post_init__(self) -> None:
        if not 0.0 < self.entry_weight <= 1.0:
            raise ValueError(f"entry_weight must be in (0, 1]: {self.entry_weight}")
        if self.exit_history_bars < 1:
            raise ValueError(f"exit_history_bars must be >= 1: {self.exit_history_bars}")


def build_target(ctx: Context, config: StrategyConfig | None = None) -> Target:
    cfg = config or StrategyConfig()
    items: list[TargetItem] = []
    seen: set[str] = set()

    for pos in ctx.positions:
        items.append(_held_item(pos, ctx, cfg))
        seen.add(pos.symbol)

    for symbol, event_id, exit_rule in _ranked_entries(ctx, seen):
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
    signal = exit_rules.evaluate(pos, bars, now=ctx.now)
    if signal is not None:
        return TargetItem(
            symbol=pos.symbol, weight=0.0, urgency=Urgency.IMMEDIATE, event_id=pos.event_id
        )
    return TargetItem(
        symbol=pos.symbol,
        weight=_current_weight(pos, ctx, cfg),
        urgency=Urgency.NEXT_OPEN,
        exit_rule=pos.exit_rule,
        event_id=pos.event_id,
    )


def _current_weight(pos: Position, ctx: Context, cfg: StrategyConfig) -> float:
    if ctx.equity <= 0:
        return cfg.entry_weight
    bar = ctx.bars.latest(pos.symbol)
    if bar is None or bar.close <= 0:
        return cfg.entry_weight
    return min(max(pos.qty * bar.close / ctx.equity, 0.0), 1.0)


def _ranked_entries(ctx: Context, exclude: set[str]) -> list[tuple[str, str, ExitRule]]:
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
