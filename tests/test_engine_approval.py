"""engine/approval.py 테스트 (구현 계획 6단계).

가장 중요하게 보는 것: (1) 같은 event_id로 여러 번 propose해도 제안이
하나만 생긴다(멱등), (2) TTL을 넘긴 제안은 승인/거부가 반영되지 않는다,
(3) pull_approved()가 같은 제안을 두 번 돌려주지 않는다(중복 진입 방지).
"""

from datetime import datetime, timedelta

import pytest

from sontrader.core.types import ExitRule, TargetItem, Urgency
from sontrader.data import db
from sontrader.engine import approval

NOW = datetime(2026, 3, 10, 9, 0)


def entry_item(*, symbol="005930", event_id="evt-1", weight=0.20) -> TargetItem:
    return TargetItem(
        symbol=symbol,
        weight=weight,
        urgency=Urgency.NEXT_OPEN,
        exit_rule=ExitRule(),
        event_id=event_id,
    )


def test_propose_creates_pending_proposal_with_ttl(db_engine):
    db.migrate(db_engine)

    proposal = approval.propose(db_engine, entry_item(), now=NOW, ttl=timedelta(hours=6))

    assert proposal.status is approval.ApprovalStatus.PENDING
    assert proposal.symbol == "005930"
    assert proposal.event_id == "evt-1"
    assert proposal.exit_rule == ExitRule()
    assert proposal.expires_at == NOW + timedelta(hours=6)


def test_propose_is_idempotent_for_same_event_id(db_engine):
    db.migrate(db_engine)

    first = approval.propose(db_engine, entry_item(), now=NOW)
    second = approval.propose(db_engine, entry_item(), now=NOW + timedelta(minutes=5))

    assert first.proposal_id == second.proposal_id
    assert len(approval.list_pending(db_engine)) == 1


def test_propose_accepts_item_without_event_id(db_engine):
    """워치리스트 순위 진입은 촉발한 이벤트가 없다 (EntryTrigger.WATCHLIST_RANK).

    예전에는 event_id를 필수로 요구해서, LLM 없이 진입하는 구성에서 승인 큐가
    사이클을 통째로 예외로 죽였다.
    """
    db.migrate(db_engine)
    item = TargetItem(
        symbol="005930", weight=0.2, urgency=Urgency.NEXT_OPEN, exit_rule=ExitRule(), event_id=None
    )

    proposal = approval.propose(db_engine, item, now=NOW)

    assert proposal.symbol == "005930"
    assert proposal.event_id is None
    assert len(approval.list_pending(db_engine)) == 1


def test_propose_without_event_id_dedups_on_symbol(db_engine):
    """이벤트가 없으면 종목이 정체성이다 — 매 사이클 제안이 쌓이면 안 된다."""
    db.migrate(db_engine)

    def item(symbol):
        return TargetItem(
            symbol=symbol,
            weight=0.2,
            urgency=Urgency.NEXT_OPEN,
            exit_rule=ExitRule(),
            event_id=None,
        )

    first = approval.propose(db_engine, item("005930"), now=NOW)
    again = approval.propose(db_engine, item("005930"), now=NOW)
    other = approval.propose(db_engine, item("000660"), now=NOW)

    assert again.proposal_id == first.proposal_id  # 같은 종목 → 재사용
    assert other.proposal_id != first.proposal_id  # 다른 종목 → 별도 제안
    assert len(approval.list_pending(db_engine)) == 2


def test_event_and_symbol_keys_do_not_collide(db_engine):
    """event_id가 우연히 종목코드와 같아도 섞이지 않는다."""
    db.migrate(db_engine)
    with_event = TargetItem(
        symbol="005930",
        weight=0.2,
        urgency=Urgency.NEXT_OPEN,
        exit_rule=ExitRule(),
        event_id="005930",
    )
    without = TargetItem(
        symbol="005930",
        weight=0.2,
        urgency=Urgency.NEXT_OPEN,
        exit_rule=ExitRule(),
        event_id=None,
    )

    a = approval.propose(db_engine, with_event, now=NOW)
    b = approval.propose(db_engine, without, now=NOW)

    assert a.proposal_id != b.proposal_id


def test_propose_rejects_item_without_exit_rule(db_engine):
    db.migrate(db_engine)
    item = TargetItem(
        symbol="005930", weight=0.2, urgency=Urgency.NEXT_OPEN, exit_rule=None, event_id="evt-1"
    )

    with pytest.raises(ValueError, match="exit_rule"):
        approval.propose(db_engine, item, now=NOW)


def test_propose_creates_new_proposal_when_existing_one_expired(db_engine):
    db.migrate(db_engine)
    stale = approval.propose(db_engine, entry_item(), now=NOW, ttl=timedelta(minutes=1))

    fresh = approval.propose(db_engine, entry_item(), now=NOW + timedelta(hours=1))

    assert fresh.proposal_id != stale.proposal_id
    assert fresh.status is approval.ApprovalStatus.PENDING
    pending_ids = {p.proposal_id for p in approval.list_pending(db_engine)}
    assert pending_ids == {fresh.proposal_id}


def test_decide_approve_transitions_to_approved(db_engine):
    db.migrate(db_engine)
    proposal = approval.propose(db_engine, entry_item(), now=NOW)

    decided = approval.decide(db_engine, proposal.proposal_id, approve=True, now=NOW)

    assert decided.status is approval.ApprovalStatus.APPROVED
    assert approval.list_pending(db_engine) == []


def test_decide_reject_transitions_to_rejected(db_engine):
    db.migrate(db_engine)
    proposal = approval.propose(db_engine, entry_item(), now=NOW)

    decided = approval.decide(db_engine, proposal.proposal_id, approve=False, now=NOW)

    assert decided.status is approval.ApprovalStatus.REJECTED


def test_decide_raises_when_already_decided(db_engine):
    db.migrate(db_engine)
    proposal = approval.propose(db_engine, entry_item(), now=NOW)
    approval.decide(db_engine, proposal.proposal_id, approve=True, now=NOW)

    with pytest.raises(approval.ProposalNotPendingError):
        approval.decide(db_engine, proposal.proposal_id, approve=False, now=NOW)


def test_decide_raises_and_expires_when_ttl_passed(db_engine):
    db.migrate(db_engine)
    proposal = approval.propose(db_engine, entry_item(), now=NOW, ttl=timedelta(hours=1))

    with pytest.raises(approval.ProposalNotPendingError):
        approval.decide(db_engine, proposal.proposal_id, approve=True, now=NOW + timedelta(hours=2))

    assert approval.list_pending(db_engine) == []


def test_decide_raises_for_unknown_proposal_id(db_engine):
    db.migrate(db_engine)

    with pytest.raises(approval.ProposalNotFoundError):
        approval.decide(db_engine, "does-not-exist", approve=True, now=NOW)


def test_expire_stale_marks_pending_past_ttl_as_expired_and_returns_them(db_engine):
    db.migrate(db_engine)
    proposal = approval.propose(db_engine, entry_item(), now=NOW, ttl=timedelta(hours=1))

    expired = approval.expire_stale(db_engine, now=NOW + timedelta(hours=2))

    assert [p.proposal_id for p in expired] == [proposal.proposal_id]
    assert expired[0].status is approval.ApprovalStatus.EXPIRED
    assert approval.list_pending(db_engine) == []


def test_expire_stale_leaves_fresh_pending_untouched(db_engine):
    db.migrate(db_engine)
    approval.propose(db_engine, entry_item(), now=NOW, ttl=timedelta(hours=6))

    expired = approval.expire_stale(db_engine, now=NOW + timedelta(hours=1))

    assert expired == []
    assert len(approval.list_pending(db_engine)) == 1


def test_pull_approved_consumes_and_returns_approved_proposals(db_engine):
    db.migrate(db_engine)
    proposal = approval.propose(db_engine, entry_item(), now=NOW)
    approval.decide(db_engine, proposal.proposal_id, approve=True, now=NOW)

    pulled = approval.pull_approved(db_engine)

    assert [p.proposal_id for p in pulled] == [proposal.proposal_id]
    assert pulled[0].status is approval.ApprovalStatus.APPROVED


def test_pull_approved_does_not_return_same_proposal_twice(db_engine):
    db.migrate(db_engine)
    proposal = approval.propose(db_engine, entry_item(), now=NOW)
    approval.decide(db_engine, proposal.proposal_id, approve=True, now=NOW)
    approval.pull_approved(db_engine)

    assert approval.pull_approved(db_engine) == []


def test_list_pending_only_returns_pending(db_engine):
    db.migrate(db_engine)
    pending = approval.propose(db_engine, entry_item(event_id="evt-pending"), now=NOW)
    approved = approval.propose(db_engine, entry_item(event_id="evt-approved"), now=NOW)
    approval.decide(db_engine, approved.proposal_id, approve=True, now=NOW)

    result = approval.list_pending(db_engine)

    assert [p.proposal_id for p in result] == [pending.proposal_id]
