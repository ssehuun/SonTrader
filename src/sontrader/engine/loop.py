"""공용 실행 루프 (구현 계획 5단계, 승인 큐/킬 스위치 연결은 6단계). 02문서 §3.1.

"1분봉 1틱 처리. 실전과 백테스트가 동일하게 호출한다." strategy → gate →
승인 큐 → diff는 순수(core, 승인 큐 부분만 예외)고, 마지막 한 줄만
부작용이다(브로커 제출). 이 함수가 "engine/loop.py가 하나뿐"이라는 설계의
핵심을 코드로 만든다 — `apps/live.py`와 `apps/backtest.py`는 서로 다른
`Broker` 구현(`broker_kis` vs `broker_sim`)과 서로 다른 `Context` 조립
방식만 주입하고, 사이클 처리 로직 자체는 여기 하나뿐이다.

## 신규 진입만 승인 큐를 거친다

청산은 완전 자동이라는 집행 비대칭(설계 1.3절)이 여기서도 유지된다.
게이트를 통과한 목표 중 "이미 보유 중인 종목"(청산 포함)은 그대로
통과시키고, "아직 보유하지 않은 종목에 대한 양의 비중"(신규 진입 후보)만
`engine/approval.py`에 제안으로 올린다. 그 사이클에 바로 주문이 되는 게
아니라, 사람이 텔레그램으로 승인한 뒤 **다음 사이클**에 `pull_approved()`로
집어져 나와서야 주문이 된다 — 그래서 `CycleResult.target`은 "이번 사이클에
새로 제안된 것"이 아니라 "보유분 + 이번에 새로 승인 확정된 것"이다.

## `require_approval=False`가 필요한 이유

백테스트는 사람이 없다. 결정적이어야 하므로 승인 큐를 거치면 안 된다 —
그래서 `CycleConfig.require_approval=False`(`apps/backtest.py`가 쓴다)면
게이트를 통과한 목표를 그대로 주문으로 흘려보내고 `Deps.engine`/`notifier`도
쓰지 않는다. 이건 01문서 §2.5 "자동 전환 시 이 단계만 비활성화"와 같은
스위치이기도 하다 — 전략이 검증되면 실전에서도 승인 큐를 끌 수 있다.

## 킬 스위치는 제안도, 승인 픽업도 막는다

`killswitch.is_engaged()`가 True면 이번 사이클은 신규 진입 후보를 제안하지
않고, 이미 승인된 채 대기 중인 제안도 픽업하지 않는다 — 둘 다 막아야
"지금 당장 신규 진입을 전부 멈춘다"는 킬 스위치의 의도가 선다. 이미 승인된
제안은 소멸하지 않고 큐에 그대로 남아, 킬 스위치가 풀리면 다음 사이클에
picked up된다.

## 주문이 없어도 매 사이클 `broker.submit()`을 부른다

`orders`가 빈 리스트여도 호출한다 — `Broker` 프로토콜 문서(`adapters/broker.py`)가
명시하듯, 시뮬레이션 구현은 이 호출을 신호 삼아 D+2 정산을 진행한다. 호출을
건너뛰면 정산도 멈춘다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy.engine import Engine

from sontrader.adapters.broker import Broker, OrderResult
from sontrader.adapters.notifier_tg import Notifier
from sontrader.core import diff, gate, strategy
from sontrader.core.diff import DiffConfig
from sontrader.core.gate import GateConfig, Rejection
from sontrader.core.strategy import StrategyConfig
from sontrader.core.types import Context, Order, Target, TargetItem, Urgency
from sontrader.engine import approval, killswitch


@dataclass(frozen=True)
class Deps:
    broker: Broker
    # 승인 큐/킬 스위치 상태 저장소. require_approval=True(기본값)면 필수.
    engine: Engine | None = None
    # 승인 요청·만료 알림 발송. None이면 조용히 생략한다(알림 미설정 상태로도
    # 승인 큐 자체는 동작해야 하므로 필수로 만들지 않는다).
    notifier: Notifier | None = None


@dataclass(frozen=True)
class CycleConfig:
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    gate: GateConfig = field(default_factory=GateConfig)
    diff: DiffConfig = field(default_factory=DiffConfig)
    require_approval: bool = True
    approval_ttl: timedelta = approval.DEFAULT_TTL


@dataclass(frozen=True)
class CycleResult:
    target: Target  # 게이트+승인 큐 통과 후 최종 목표 (원본 전략 출력이 아니다)
    rejections: tuple[Rejection, ...]
    orders: tuple[Order, ...]
    order_results: tuple[OrderResult, ...]


def run_cycle(ctx: Context, deps: Deps, config: CycleConfig | None = None) -> CycleResult:
    cfg = config or CycleConfig()
    if cfg.require_approval and deps.engine is None:
        raise ValueError("CycleConfig.require_approval=True requires Deps.engine")

    raw_target = strategy.build_target(ctx, cfg.strategy)
    gated = gate.apply(raw_target, ctx, cfg.gate)
    final_target = Target(_resolve_entries(gated.target, ctx, deps, cfg))
    orders = diff.to_orders(final_target, ctx, cfg.diff)
    order_results = deps.broker.submit(orders, now=ctx.now)

    return CycleResult(
        target=final_target,
        rejections=gated.rejections,
        orders=tuple(orders),
        order_results=tuple(order_results),
    )


def _resolve_entries(
    target: Target, ctx: Context, deps: Deps, cfg: CycleConfig
) -> tuple[TargetItem, ...]:
    if not cfg.require_approval:
        return target.items

    held = ctx.held_symbols
    auto_items = tuple(item for item in target if item.symbol in held or item.weight <= 0.0)

    if killswitch.is_engaged(deps.engine):
        return auto_items

    for item in target:
        if item.symbol in held or item.weight <= 0.0:
            continue
        proposal, created = approval.propose(deps.engine, item, now=ctx.now, ttl=cfg.approval_ttl)
        # 새로 만든 제안만 알린다. build_target()이 승인 전까지 매 사이클 같은
        # 후보를 다시 내놓으므로, created를 보지 않으면 60초마다 같은 요청이
        # 재전송된다.
        if created and deps.notifier is not None:
            deps.notifier.send_approval_request(proposal)

    expired = approval.expire_stale(deps.engine, now=ctx.now)
    if deps.notifier is not None:
        for proposal in expired:
            deps.notifier.send_message(f"승인 요청 만료: {proposal.symbol}")

    approved_items = tuple(_to_target_item(p) for p in approval.pull_approved(deps.engine))
    return auto_items + approved_items


def _to_target_item(proposal: approval.Proposal) -> TargetItem:
    return TargetItem(
        symbol=proposal.symbol,
        weight=proposal.weight,
        urgency=Urgency.NEXT_OPEN,
        exit_rule=proposal.exit_rule,
        event_id=proposal.event_id,
    )
