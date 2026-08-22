"""리스크 게이트 (구현 계획 4단계). 순수 함수 — 시각·상태는 `Context`로 주입된다.

설계 2.5절의 게이트를 구현한다. 설계 1절이 "게이트는 1급 컴포넌트"라고 못박은
이유는 하나다 — 자동 신호 생성 시스템은 방치하면 반드시 과매매로 간다.

## 이 모듈이 담당하는 규칙 (설계 2.5절 표)

| 규칙 | 값 |
|---|---|
| 최대 보유 종목 수 | 5 |
| 종목당 최대 비중 | 20% |
| 신호 경합 | 슬롯이 차 있으면 신규 신호 스킵. **교체 없음** |
| 동일 이벤트 재진입 | 금지 (event_id 기준) |
| 시간 기반 쿨다운 | 파라미터 (기본 0 = 비활성, 설계 8절 백테스트로 확정) |

같은 표의 나머지는 여기 없다. no-trade band와 최소 주문금액은 (목표 − 현재)
편차에 걸리는 규칙이라 `core/diff.py`의 몫이고, D+2 결제·장 운영시간·휴장일·
킬 스위치는 캘린더나 외부 상태가 필요해 core에 둘 수 없다 (구조 원칙 1).

## 게이트는 청산을 절대 막지 않는다

게이트는 **노출을 늘리는 방향으로만** 작동한다. 비중 0(청산)이나 목표에서
빠진 보유 종목에는 손대지 않는다. 설계 1.3절의 집행 비대칭과 같은 논리다 —
청산을 막는 리스크 관리는 리스크 관리가 아니다.

## 슬롯이 경합할 때의 우선순위

게이트는 **입력 순서를 그대로 존중한다.** 어느 신호가 더 좋은지는 게이트가
아니라 전략이 아는 정보이므로, 우선순위 결정은 `build_target()`이 항목을
나열하는 순서에 맡긴다. 게이트가 재정렬하면 같은 목표에 대해 게이트 설정만
바꿔도 어느 종목이 들어갈지가 달라져 백테스트 해석이 어려워진다.

비중 상한(20%) × 슬롯 상한(5) = 100%이므로, 통과한 목표의 비중 합은 1.0을
넘지 않는다. 별도 정규화가 필요 없다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from sontrader.core.types import Context, Target, TargetItem


@dataclass(frozen=True)
class GateConfig:
    """게이트 파라미터. core는 설정 파일을 읽지 않으므로 주입받는다."""

    max_positions: int = 5
    max_weight: float = 0.20
    # 청산 후 같은 종목에 재진입하기까지의 최소 간격(달력일). 0이면 비활성.
    # 거래일이 아니라 달력일인 것은 `core/exit_rules.py`의 max_hold_days와 같은
    # 이유다 — core는 휴장일 캘린더를 알지 않는다.
    cooldown_days: int = 0

    def __post_init__(self) -> None:
        if self.max_positions < 1:
            raise ValueError(f"max_positions must be >= 1: {self.max_positions}")
        if not 0.0 < self.max_weight <= 1.0:
            raise ValueError(f"max_weight must be in (0, 1]: {self.max_weight}")
        if self.cooldown_days < 0:
            raise ValueError(f"cooldown_days must be >= 0: {self.cooldown_days}")


class RejectReason(str, Enum):
    SLOT_FULL = "slot_full"  # 보유 종목 수 상한 — 교체 없이 스킵
    DUPLICATE_EVENT = "duplicate_event"  # 동일 이벤트로 이미 진입한 적 있음
    COOLDOWN = "cooldown"  # 직전 청산 이후 쿨다운 미경과
    # 킬 스위치는 core 밖(engine/loop.py)에서 붙인다. 이 enum에 값만 두는 이유는
    # "왜 안 샀나"의 사유 목록이 한 군데 모여 있어야 하기 때문이다(01문서 §6.6.1).
    KILL_SWITCH = "kill_switch"


@dataclass(frozen=True)
class Rejection:
    """스킵된 신규 진입 1건. 텔레그램 알림과 백테스트 진단의 입력이다."""

    symbol: str
    reason: RejectReason
    event_id: str | None = None


@dataclass(frozen=True)
class GateResult:
    target: Target
    rejections: tuple[Rejection, ...] = ()


def apply(target: Target, ctx: Context, config: GateConfig | None = None) -> GateResult:
    """목표 포트폴리오에 게이트를 적용한다.

    02 문서 §3.1의 `gate.apply(target, ctx) -> Target`에서 반환형만 넓혔다.
    무엇이 왜 막혔는지는 텔레그램 알림과 백테스트 진단에 반드시 필요한데,
    Target만 돌려주면 그 정보가 사라진다. 호출부는 `.target`을 쓴다.
    """
    cfg = config or GateConfig()
    held = ctx.held_symbols

    # 동일 이벤트 재진입 금지의 근거 집합. 청산된 포지션까지 포함해야 하므로
    # 보유분의 event_id만으로는 부족하다 — 엔진이 채워 넣는 이력을 함께 본다.
    used_events = set(ctx.used_event_ids)
    used_events.update(pos.event_id for pos in ctx.positions if pos.event_id)

    # 목표에 비중 > 0으로 남아 있는 보유 종목만 슬롯을 계속 점유한다.
    # 목표에서 빠졌거나 비중 0인 보유 종목은 청산 대상이므로 슬롯을 비운다.
    occupied = len(held & {item.symbol for item in target if item.weight > 0})

    kept: list[TargetItem] = []
    rejections: list[Rejection] = []

    for item in target:
        if item.symbol in held:
            # 보유 종목은 게이트가 막지 않는다 (청산 방해 금지, 교체 없음).
            # 비중 상한만 적용한다 — 상한 초과는 노출을 줄이는 방향이라 안전하다.
            kept.append(_clamp(item, cfg.max_weight))
            continue

        if item.weight <= 0.0:
            # 보유하지도 않은 종목의 비중 0 — 청산할 것이 없다. 조용히 버린다.
            continue

        if item.event_id is not None and item.event_id in used_events:
            rejections.append(Rejection(item.symbol, RejectReason.DUPLICATE_EVENT, item.event_id))
            continue

        if _in_cooldown(item.symbol, ctx, cfg):
            rejections.append(Rejection(item.symbol, RejectReason.COOLDOWN, item.event_id))
            continue

        if occupied >= cfg.max_positions:
            rejections.append(Rejection(item.symbol, RejectReason.SLOT_FULL, item.event_id))
            continue

        kept.append(_clamp(item, cfg.max_weight))
        occupied += 1
        if item.event_id is not None:
            # 같은 사이클 안에서 한 이벤트가 두 항목을 만들어도 한 번만 통과시킨다.
            used_events.add(item.event_id)

    return GateResult(Target(tuple(kept)), tuple(rejections))


def _clamp(item: TargetItem, max_weight: float) -> TargetItem:
    if item.weight <= max_weight:
        return item
    return TargetItem(
        symbol=item.symbol,
        weight=max_weight,
        urgency=item.urgency,
        exit_rule=item.exit_rule,
        event_id=item.event_id,
    )


def _in_cooldown(symbol: str, ctx: Context, cfg: GateConfig) -> bool:
    if cfg.cooldown_days <= 0:
        return False
    last_exit = ctx.last_exit_at.get(symbol)
    if last_exit is None:
        return False
    return (ctx.now.date() - last_exit.date()).days < cfg.cooldown_days
