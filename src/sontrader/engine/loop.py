"""공용 실행 루프 (구현 계획 5단계, 킬 스위치 연결은 6단계). 02문서 §3.1.

"1분봉 1틱 처리. 실전과 백테스트가 동일하게 호출한다." strategy → gate →
킬 스위치 → diff는 순수(core, 킬 스위치 조회만 예외)고, 마지막 한 줄만
부작용이다(브로커 제출). 이 함수가 "engine/loop.py가 하나뿐"이라는 설계의
핵심을 코드로 만든다 — `apps/live.py`와 `apps/backtest.py`는 서로 다른
`Broker` 구현(`broker_kis` vs `broker_sim`)과 서로 다른 `Context` 조립
방식만 주입하고, 사이클 처리 로직 자체는 여기 하나뿐이다.

## 사람의 승인을 받지 않는다

초안에는 진입 후보를 텔레그램으로 사람에게 승인받는 큐(`engine/approval.py`)가
있었으나 삭제했다. 사람이 건별로 거부하면 실전에서 도는 것은 백테스트가
검증한 전략이 아니게 되고, "백테스트와 실전은 동일한 전략·게이트 코드를
실행한다"(01문서 §1.1 원칙 1)가 깨진다. 게이트를 통과한 목표는 그 사이클에
곧바로 주문이 된다.

## 킬 스위치는 신규 진입만 막는다

사람이 개입할 수 있는 유일한 지점이고, 종목을 고르는 수단이 아니라 시스템
전체를 세우는 수단이다. `killswitch.is_engaged()`가 True면 "아직 보유하지
않은 종목에 대한 양의 비중"(신규 진입)만 떨어내고, 보유 종목의 축소·청산은
그대로 통과시킨다 — 청산까지 멈추면 리스크 관리가 아니라 리스크 방치가
된다(집행 비대칭, 01문서 §1.3).

막힌 후보는 조용히 사라지지 않고 `RejectReason.KILL_SWITCH`로
`CycleResult.rejections`에 남는다. "그날 왜 안 샀나"에 답하려면 슬롯이
찼는지 킬 스위치였는지를 구분할 수 있어야 한다(01문서 §6.6.1).

## `check_killswitch=False`가 필요한 이유

백테스트에는 조작할 사람도 상태 저장소도 없다 — 킬 스위치는 항상 꺼진
상태다. 그래서 `apps/backtest.py`는 이 플래그를 끄고 `Deps.engine` 없이
돈다. 반대로 기본값이 True인 덕에, 실전에서 `engine`을 빠뜨리면 킬 스위치가
조용히 무력화되는 대신 기동 시점에 `ValueError`로 죽는다(fail-closed).

## 주문이 없어도 매 사이클 `broker.submit()`을 부른다

`orders`가 빈 리스트여도 호출한다 — `Broker` 프로토콜 문서(`adapters/broker.py`)가
명시하듯, 시뮬레이션 구현은 이 호출을 신호 삼아 D+2 정산을 진행한다. 호출을
건너뛰면 정산도 멈춘다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.engine import Engine

from sontrader.adapters.broker import Broker, OrderResult
from sontrader.core import diff, gate, strategy
from sontrader.core.diff import DiffConfig
from sontrader.core.gate import GateConfig, Rejection, RejectReason
from sontrader.core.strategy import StrategyConfig
from sontrader.core.types import Context, Order, Target, TargetItem
from sontrader.engine import killswitch


@dataclass(frozen=True)
class Deps:
    broker: Broker
    # 킬 스위치 상태 저장소. check_killswitch=True(기본값)면 필수.
    engine: Engine | None = None


@dataclass(frozen=True)
class CycleConfig:
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    gate: GateConfig = field(default_factory=GateConfig)
    diff: DiffConfig = field(default_factory=DiffConfig)
    check_killswitch: bool = True


@dataclass(frozen=True)
class CycleResult:
    target: Target  # 게이트+킬 스위치 통과 후 최종 목표 (원본 전략 출력이 아니다)
    rejections: tuple[Rejection, ...]
    orders: tuple[Order, ...]
    order_results: tuple[OrderResult, ...]


def run_cycle(ctx: Context, deps: Deps, config: CycleConfig | None = None) -> CycleResult:
    cfg = config or CycleConfig()
    if cfg.check_killswitch and deps.engine is None:
        raise ValueError("CycleConfig.check_killswitch=True requires Deps.engine")

    raw_target = strategy.build_target(ctx, cfg.strategy)
    gated = gate.apply(raw_target, ctx, cfg.gate)
    items, blocked = _apply_killswitch(gated.target, ctx, deps, cfg)

    final_target = Target(items)
    orders = diff.to_orders(final_target, ctx, cfg.diff)
    order_results = deps.broker.submit(orders, now=ctx.now)

    return CycleResult(
        target=final_target,
        rejections=gated.rejections + blocked,
        orders=tuple(orders),
        order_results=tuple(order_results),
    )


def _apply_killswitch(
    target: Target, ctx: Context, deps: Deps, cfg: CycleConfig
) -> tuple[tuple[TargetItem, ...], tuple[Rejection, ...]]:
    """신규 진입만 떨어낸다. 보유 종목의 축소·청산은 항상 통과시킨다."""
    if not cfg.check_killswitch or not killswitch.is_engaged(deps.engine):
        return target.items, ()

    held = ctx.held_symbols
    kept: list[TargetItem] = []
    blocked: list[Rejection] = []
    for item in target:
        if item.symbol in held or item.weight <= 0.0:
            kept.append(item)
        else:
            blocked.append(Rejection(item.symbol, RejectReason.KILL_SWITCH, item.event_id))
    return tuple(kept), tuple(blocked)
