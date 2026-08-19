"""승인 큐 (구현 계획 6단계). 01문서 §2.5 "승인 큐", §6.3 "진입 승인/거부".

## 이 모듈이 담당하지 않는 것

텔레그램으로 승인 요청을 보내고 버튼 콜백을 받는 일은 `adapters/notifier_tg.py`
(다음 슬라이스)의 몫이다. 여기서는 그 앞단 — 제안을 만들고, 결정을 기록하고,
TTL을 만료시키는 순수 DB 로직만 다룬다. 신규 진입 후보를 `propose()`로 밀어
넣고 `pull_approved()`로 확정된 것만 꺼내가는 흐름을 `engine/loop.py`에 잇는
일도 다음 슬라이스다 — core는 외부 상태(승인 대기열)를 알면 안 되므로
(`core/gate.py`의 "킬 스위치·승인 큐는 core에 둘 수 없다" 주석 참고) 이
모듈은 engine 계층에 있다.

## 무엇으로 중복을 막는가

`core/strategy.py`의 `build_target()`은 승인이 나기 전까지 매 사이클 같은
진입 후보를 계속 다시 내놓는다(승인 여부를 알지 못하니 당연하다).
`propose()`를 멱등하게 만들지 않으면 사이클마다 같은 종목에 제안이 쌓인다.

식별자는 **이벤트가 있으면 event_id, 없으면 종목코드**다(`_proposal_key`).
이벤트 기반 진입(EntryTrigger.EVENT)은 이벤트당 판단이 하나뿐이라
(Judgment 캐시, 4단계) event_id가 자연스러운 정체성이고, 같은 종목에 다른
이벤트가 새로 발생하면 별도 제안으로 취급한다. 반면 워치리스트 순위 기반
진입(EntryTrigger.WATCHLIST_RANK)은 촉발한 이벤트가 없으므로 종목 자체가
정체성이다 — 이쪽은 한 종목에 제안이 하나만 대기하게 되어 더 엄격하다.

## TTL 경합

`decide()`는 상태가 `pending`인지만 보지 않고 `now >= expires_at`도 함께
확인한다. `expire_stale()`을 얼마나 자주 돌리든, 스윕이 돌기 직전에 버튼이
눌리는 경합을 이 이중 확인이 닫는다 — 버튼을 누른 시점이 이미 TTL을
넘겼다면 그 결정은 반영되지 않고 제안은 즉시 만료 처리된다. 같은 이유로
`propose()`도 같은 event_id의 기존 제안이 TTL을 넘긴 채 아직 스윕되지
않았다면 그걸 재사용하지 않고 만료 처리한 뒤 새 제안을 만든다.

## `approved`가 최종 상태가 아닌 이유

승인된 제안이 실제 주문으로 바뀌는 것은 다음 사이클의 몫이다. `approved`로
영원히 남겨두면 그다음 사이클들도 계속 "승인됨"으로 보여서 매 사이클 중복
주문을 만들게 된다. `pull_approved()`는 승인된 제안을 반환함과 동시에
`consumed`로 바꾸는 원자적 pop이라, 같은 제안이 두 번 주문으로 전환되는
일이 없다.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from sontrader.core.types import ExitRule, TargetItem
from sontrader.data import db

DEFAULT_TTL = timedelta(hours=12)  # 다음 개장 시가 집행 전에는 결정돼야 한다


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CONSUMED = "consumed"  # 승인 후 실제 주문으로 전환됨 — 더 이상 대기열에 없음


class ProposalNotFoundError(RuntimeError):
    pass


class ProposalNotPendingError(RuntimeError):
    """이미 결정됐거나 만료된 제안에 다시 결정을 내리려 했다."""


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    symbol: str
    weight: float
    exit_rule: ExitRule
    event_id: str
    status: ApprovalStatus
    expires_at: datetime


def _proposal_key(symbol: str, event_id: str | None) -> str:
    """제안 중복 판정의 정체성. 이벤트가 있으면 그것이, 없으면 종목이 기준이다.

    접두어를 붙이는 이유는 event_id가 우연히 종목코드와 같은 문자열이어도
    섞이지 않게 하기 위해서다.
    """
    return f"event:{event_id}" if event_id is not None else f"symbol:{symbol}"


def propose(
    engine: Engine, item: TargetItem, *, now: datetime, ttl: timedelta = DEFAULT_TTL
) -> Proposal:
    """진입 후보 하나를 대기열에 넣는다.

    같은 후보(모듈 상단 `_proposal_key` 참고)로 대기 중인 제안이 있으면 새로
    만들지 않고 그걸 그대로 반환한다(멱등). 단, 그 기존 제안이 TTL을 넘겼다면
    재사용하지 않고 먼저 만료 처리한 뒤 새로 만든다.
    """
    if item.exit_rule is None:
        raise ValueError("approval proposals require an exit_rule (entry candidates only)")
    if item.weight <= 0:
        raise ValueError("approval proposals require a positive weight (entry candidates only)")

    key = _proposal_key(item.symbol, item.event_id)
    for existing in _load_by_status(engine, ApprovalStatus.PENDING):
        if _proposal_key(existing.symbol, existing.event_id) != key:
            continue
        if now >= existing.expires_at:
            _set_status(engine, existing.proposal_id, ApprovalStatus.EXPIRED)
            continue
        return existing

    proposal_id = str(uuid.uuid4())
    expires_at = now + ttl
    with engine.begin() as conn:
        conn.execute(
            db.approvals.insert().values(
                proposal_id=proposal_id,
                payload_json=_to_payload(item),
                status=ApprovalStatus.PENDING.value,
                expires_at=expires_at,
            )
        )
    return Proposal(
        proposal_id,
        item.symbol,
        item.weight,
        item.exit_rule,
        item.event_id,
        ApprovalStatus.PENDING,
        expires_at,
    )


def decide(engine: Engine, proposal_id: str, *, approve: bool, now: datetime) -> Proposal:
    """승인(``approve=True``) 또는 거부.

    TTL을 넘긴 제안은 승인/거부 요청과 관계없이 만료 처리하고 예외를 던진다
    — 다음 개장 시가를 이미 지났을 수 있는 결정을 조용히 반영하면 의도치
    않은 진입이 나간다(fail-closed).
    """
    row = _load_row(engine, proposal_id)
    if row is None:
        raise ProposalNotFoundError(proposal_id)

    proposal = _to_proposal(row)
    if proposal.status is not ApprovalStatus.PENDING:
        raise ProposalNotPendingError(
            f"proposal {proposal_id} is {proposal.status.value}, not pending"
        )
    if now >= proposal.expires_at:
        _set_status(engine, proposal_id, ApprovalStatus.EXPIRED)
        raise ProposalNotPendingError(f"proposal {proposal_id} expired at {proposal.expires_at}")

    new_status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
    _set_status(engine, proposal_id, new_status)
    return Proposal(
        proposal.proposal_id,
        proposal.symbol,
        proposal.weight,
        proposal.exit_rule,
        proposal.event_id,
        new_status,
        proposal.expires_at,
    )


def expire_stale(engine: Engine, *, now: datetime) -> list[Proposal]:
    """TTL을 넘긴 대기 중 제안을 전부 만료 처리하고, 그 목록을 돌려준다
    (호출자가 텔레그램으로 "만료됨" 알림을 보낼 수 있도록)."""
    expired = []
    for proposal in _load_by_status(engine, ApprovalStatus.PENDING):
        if now < proposal.expires_at:
            continue
        _set_status(engine, proposal.proposal_id, ApprovalStatus.EXPIRED)
        expired.append(
            Proposal(
                proposal.proposal_id,
                proposal.symbol,
                proposal.weight,
                proposal.exit_rule,
                proposal.event_id,
                ApprovalStatus.EXPIRED,
                proposal.expires_at,
            )
        )
    return expired


def pull_approved(engine: Engine) -> list[Proposal]:
    """승인된 제안을 전부 꺼내가면서 즉시 `consumed`로 바꾼다.

    다음 사이클에 같은 제안이 다시 주문으로 전환되는(중복 진입) 일을 막는
    원자적 pop이다.
    """
    approved = _load_by_status(engine, ApprovalStatus.APPROVED)
    for proposal in approved:
        _set_status(engine, proposal.proposal_id, ApprovalStatus.CONSUMED)
    return approved


def list_pending(engine: Engine) -> list[Proposal]:
    """대기 중인 제안 전체 — 텔레그램 상태 조회 명령용. 상태를 바꾸지 않는다."""
    return _load_by_status(engine, ApprovalStatus.PENDING)


def _to_payload(item: TargetItem) -> dict[str, Any]:
    return {
        "symbol": item.symbol,
        "weight": item.weight,
        "event_id": item.event_id,
        "exit_rule": item.exit_rule.to_dict(),
    }


def _to_proposal(row) -> Proposal:
    payload = row.payload_json
    return Proposal(
        proposal_id=row.proposal_id,
        symbol=payload["symbol"],
        weight=payload["weight"],
        exit_rule=ExitRule.from_dict(payload["exit_rule"]),
        event_id=payload["event_id"],
        status=ApprovalStatus(row.status),
        expires_at=row.expires_at,
    )


def _load_row(engine: Engine, proposal_id: str):
    with engine.connect() as conn:
        return conn.execute(
            sa.select(db.approvals).where(db.approvals.c.proposal_id == proposal_id)
        ).first()


def _load_by_status(engine: Engine, status: ApprovalStatus) -> list[Proposal]:
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(db.approvals).where(db.approvals.c.status == status.value)
        ).all()
    return [_to_proposal(row) for row in rows]


def _set_status(engine: Engine, proposal_id: str, status: ApprovalStatus) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.update(db.approvals)
            .where(db.approvals.c.proposal_id == proposal_id)
            .values(status=status.value)
        )
