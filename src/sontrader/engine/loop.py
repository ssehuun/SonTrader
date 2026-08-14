"""공용 실행 루프 (구현 계획 5단계). 02문서 §3.1.

"1분봉 1틱 처리. 실전과 백테스트가 동일하게 호출한다." strategy → gate →
diff는 순수(core)고, 마지막 한 줄만 부작용이다(브로커 제출). 이 함수가
"engine/loop.py가 하나뿐"이라는 설계의 핵심을 코드로 만든다 —
`apps/live.py`와 `apps/backtest.py`(다음 슬라이스)는 서로 다른 `Broker`
구현(`broker_kis` vs `broker_sim`)과 서로 다른 `Context` 조립 방식만
주입하고, 사이클 처리 로직 자체는 여기 하나뿐이다.

## `Deps`가 `broker` 하나뿐인 이유

`LLMJudge`는 4단계, `Notifier`/`ApprovalQueue`는 6단계 항목이라 아직
없다. 지금은 진입 판단이 이미 `Context.judgments`에 채워져 있다고
가정한다 — 전략은 그걸 조회만 한다(`core/strategy.py` 참고, core는 LLM을
직접 호출하지 않는다). 그 어댑터들이 생기면 `Deps`에 필드가 늘어나겠지만,
지금 없는 것을 미리 만들어두지 않는다(YAGNI, 02문서 §7).

## 주문이 없어도 매 사이클 `broker.submit()`을 부른다

`orders`가 빈 리스트여도 호출한다 — `Broker` 프로토콜 문서(`adapters/broker.py`)가
명시하듯, 시뮬레이션 구현은 이 호출을 신호 삼아 D+2 정산을 진행한다. 호출을
건너뛰면 정산도 멈춘다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sontrader.adapters.broker import Broker, OrderResult
from sontrader.core import diff, gate, strategy
from sontrader.core.diff import DiffConfig
from sontrader.core.gate import GateConfig, Rejection
from sontrader.core.strategy import StrategyConfig
from sontrader.core.types import Context, Order, Target


@dataclass(frozen=True)
class Deps:
    broker: Broker


@dataclass(frozen=True)
class CycleConfig:
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    gate: GateConfig = field(default_factory=GateConfig)
    diff: DiffConfig = field(default_factory=DiffConfig)


@dataclass(frozen=True)
class CycleResult:
    target: Target  # 게이트 통과 후 최종 목표 (원본 전략 출력이 아니다)
    rejections: tuple[Rejection, ...]
    orders: tuple[Order, ...]
    order_results: tuple[OrderResult, ...]


def run_cycle(ctx: Context, deps: Deps, config: CycleConfig | None = None) -> CycleResult:
    cfg = config or CycleConfig()

    raw_target = strategy.build_target(ctx, cfg.strategy)
    gated = gate.apply(raw_target, ctx, cfg.gate)
    orders = diff.to_orders(gated.target, ctx, cfg.diff)
    order_results = deps.broker.submit(orders, now=ctx.now)

    return CycleResult(
        target=gated.target,
        rejections=gated.rejections,
        orders=tuple(orders),
        order_results=tuple(order_results),
    )
