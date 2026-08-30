"""리스크 게이트 테스트 (구현 계획 4단계).

설계 2.5절 표의 규칙마다 최소 하나씩, 그리고 게이트에서 가장 위험한 실패
모드인 **청산 방해**를 여러 각도로 확인한다. 게이트가 청산을 막으면 리스크
관리가 리스크 원인이 된다.
"""

from datetime import datetime, timedelta

import pytest

from sontrader.core.gate import GateConfig, RejectReason, apply
from sontrader.core.types import (
    Bar,
    Context,
    ExitRule,
    Position,
    Target,
    TargetItem,
    Urgency,
)

NOW = datetime(2026, 3, 10, 9, 30)


class EmptyBars:
    """게이트는 봉을 보지 않는다 — Context를 채우기 위한 최소 구현."""

    def history(self, symbol: str, count: int) -> list[Bar]:
        return []

    def latest(self, symbol: str) -> Bar | None:
        return None


def make_ctx(**overrides) -> Context:
    base = dict(now=NOW, bars=EmptyBars(), watchlist=())
    return Context(**{**base, **overrides})


def make_position(symbol: str, *, event_id: str | None = None) -> Position:
    return Position(
        symbol=symbol,
        qty=10,
        avg_price=10_000.0,
        entered_at=NOW - timedelta(days=3),
        exit_rule=ExitRule(),
        event_id=event_id,
    )


# 기본값을 직접 읽는다. 증거금 제약 때문에 바뀌는 값이라(`core/gate.py`),
# 숫자를 박으면 정책이 바뀔 때마다 무관한 테스트가 깨진다.
SLOTS = GateConfig().max_positions
CAP = GateConfig().max_weight


def entry(symbol: str, weight: float = 0.2, *, event_id: str | None = None) -> TargetItem:
    return TargetItem(
        symbol=symbol,
        weight=weight,
        urgency=Urgency.NEXT_OPEN,
        exit_rule=ExitRule(),
        event_id=event_id,
    )


def exit_item(symbol: str) -> TargetItem:
    return TargetItem(symbol=symbol, weight=0.0, urgency=Urgency.IMMEDIATE)


# --- 청산은 절대 막지 않는다 --------------------------------------------------


def test_exit_passes_through_even_when_slots_are_full():
    held = [make_position(f"00{i}") for i in range(SLOTS)]
    ctx = make_ctx(positions=tuple(held))
    # 5슬롯이 모두 차 있고, 그 중 하나가 청산 대상이다.
    target = Target((exit_item("000"), *[entry(p.symbol) for p in held[1:]]))

    result = apply(target, ctx)

    kept = result.target.get("000")
    assert kept is not None
    assert kept.weight == 0.0
    assert kept.urgency is Urgency.IMMEDIATE
    assert result.rejections == ()


def test_exit_frees_a_slot_for_a_new_entry_in_the_same_cycle():
    held = [make_position(f"00{i}") for i in range(SLOTS)]
    ctx = make_ctx(positions=tuple(held))
    target = Target((exit_item("000"), *[entry(p.symbol) for p in held[1:]], entry("999")))

    result = apply(target, ctx)

    # 청산이 슬롯을 비웠으므로 신규 1건이 들어간다.
    assert result.target.get("999") is not None
    assert result.rejections == ()


def test_held_symbol_missing_from_target_is_left_alone():
    # 목표에 없는 보유 종목은 "전량 청산"이다. 게이트가 되살리지 않는다.
    ctx = make_ctx(positions=(make_position("000"),))

    result = apply(Target(()), ctx)

    assert result.target.symbols == frozenset()


# --- 최대 보유 종목 수 / 신호 경합 --------------------------------------------


def test_new_entries_beyond_the_slot_limit_are_skipped_not_swapped():
    held = tuple(make_position(f"00{i}") for i in range(5))
    ctx = make_ctx(positions=held)
    target = Target((*[entry(p.symbol) for p in held], entry("999", event_id="E9")))

    result = apply(target, ctx)

    # 교체 없음 — 보유 5종목은 그대로, 신규만 스킵된다.
    assert result.target.symbols == {p.symbol for p in held}
    assert len(result.rejections) == 1
    assert result.rejections[0].symbol == "999"
    assert result.rejections[0].reason is RejectReason.SLOT_FULL
    assert result.rejections[0].event_id == "E9"


def test_slot_limit_counts_admitted_entries_within_one_cycle():
    held = [make_position(f"00{i}") for i in range(2)]
    ctx = make_ctx(positions=tuple(held))
    # 보유 2 + 신규 (슬롯 − 2) + 초과 1건
    news = [f"10{i}" for i in range(SLOTS - 2 + 1)]
    target = Target((*[entry(p.symbol) for p in held], *[entry(s) for s in news]))

    result = apply(target, ctx)

    assert len(result.target) == SLOTS
    assert [r.symbol for r in result.rejections] == [news[-1]]


def test_input_order_decides_which_signal_wins_a_contested_slot():
    # 슬롯을 하나만 남긴다 — 그 하나를 A와 B가 다툰다.
    ctx = make_ctx(positions=tuple(make_position(f"00{i}") for i in range(SLOTS - 1)))
    held = [entry(f"00{i}") for i in range(SLOTS - 1)]

    first = apply(Target((*held, entry("A"), entry("B"))), ctx)
    second = apply(Target((*held, entry("B"), entry("A"))), ctx)

    assert first.target.get("A") is not None and first.target.get("B") is None
    assert second.target.get("B") is not None and second.target.get("A") is None


# --- 종목당 최대 비중 ---------------------------------------------------------


def test_weight_is_clamped_to_the_per_symbol_cap():
    ctx = make_ctx()

    result = apply(Target((entry("100", weight=0.5),)), ctx)

    item = result.target.get("100")
    assert item is not None
    assert item.weight == pytest.approx(CAP)


def test_clamping_preserves_the_rest_of_the_item():
    rule = ExitRule(max_hold_days=7)
    ctx = make_ctx()
    target = Target(
        (
            TargetItem(
                symbol="100",
                weight=0.9,
                urgency=Urgency.NEXT_OPEN,
                exit_rule=rule,
                event_id="E1",
            ),
        )
    )

    item = apply(target, ctx).target.get("100")

    assert item is not None
    assert item.exit_rule is rule
    assert item.event_id == "E1"
    assert item.urgency is Urgency.NEXT_OPEN


def test_held_position_is_not_clamped_down():
    """보유 종목에는 비중 상한을 걸지 않는다.

    걸면 전략의 설계가 무효화된다 — `core/strategy.py`가 수익 종목을 깎지
    않으려고 현재 비중을 목표로 돌려주는데 게이트가 다시 깎아버린다. 실측에서
    매수보유 재생이 체결 236건으로 번지고 −37.2%가 됐다(`core/gate.py` 주석).
    """
    ctx = make_ctx(positions=(make_position("000"),))

    item = apply(Target((entry("000", weight=0.4),)), ctx).target.get("000")

    assert item is not None
    assert item.weight == pytest.approx(0.4)


def test_a_new_entry_is_still_clamped_to_the_cap():
    """진입 시점의 상한은 그대로다 — 첫 배분이 커지는 것은 막는다."""
    ctx = make_ctx()

    item = apply(Target((entry("100", weight=0.4),)), ctx).target.get("100")

    assert item is not None
    assert item.weight == pytest.approx(CAP)


def test_total_weight_never_exceeds_one():
    ctx = make_ctx()
    target = Target(tuple(entry(f"10{i}", weight=1.0) for i in range(8)))

    result = apply(target, ctx)

    total = sum(item.weight for item in result.target)
    assert total <= 1.0
    # 상한이 두 번 걸린다 — 슬롯 수와 종목당 비중.
    assert total == pytest.approx(SLOTS * CAP)


# --- 동일 이벤트 재진입 금지 --------------------------------------------------


def test_event_already_used_by_a_held_position_is_rejected():
    ctx = make_ctx(positions=(make_position("000", event_id="E1"),))

    result = apply(Target((entry("100", event_id="E1"),)), ctx)

    assert len(result.target) == 0
    assert result.rejections[0].reason is RejectReason.DUPLICATE_EVENT


def test_event_used_by_a_closed_position_is_still_rejected():
    # 청산 직후 재진입이 뚫리면 안 된다 — 보유분이 아니라 이력을 봐야 한다.
    ctx = make_ctx(used_event_ids=frozenset({"E1"}))

    result = apply(Target((entry("100", event_id="E1"),)), ctx)

    assert len(result.target) == 0
    assert result.rejections[0].reason is RejectReason.DUPLICATE_EVENT


def test_one_event_cannot_admit_two_entries_in_the_same_cycle():
    ctx = make_ctx()

    result = apply(Target((entry("100", event_id="E1"), entry("101", event_id="E1"))), ctx)

    assert result.target.symbols == {"100"}
    assert result.rejections[0].symbol == "101"
    assert result.rejections[0].reason is RejectReason.DUPLICATE_EVENT


def test_entries_without_an_event_id_are_not_deduplicated():
    # event_id가 없는 신호(팩터 전략)까지 한 건으로 묶으면 안 된다.
    ctx = make_ctx()

    result = apply(Target((entry("100"), entry("101"))), ctx)

    assert result.target.symbols == {"100", "101"}


# --- 시간 기반 쿨다운 ---------------------------------------------------------


def test_cooldown_is_disabled_by_default():
    ctx = make_ctx(last_exit_at={"100": NOW - timedelta(hours=1)})

    assert len(apply(Target((entry("100"),)), ctx).target) == 1


def test_entry_within_the_cooldown_window_is_rejected():
    ctx = make_ctx(last_exit_at={"100": NOW - timedelta(days=2)})

    result = apply(Target((entry("100"),)), ctx, GateConfig(cooldown_days=3))

    assert len(result.target) == 0
    assert result.rejections[0].reason is RejectReason.COOLDOWN


def test_cooldown_boundary_uses_calendar_days():
    # 정확히 cooldown_days 경과 → 통과 (exit_rules.max_hold_days와 같은 규약).
    ctx = make_ctx(last_exit_at={"100": NOW - timedelta(days=3)})

    result = apply(Target((entry("100"),)), ctx, GateConfig(cooldown_days=3))

    assert len(result.target) == 1


def test_cooldown_does_not_block_a_symbol_that_is_still_held():
    ctx = make_ctx(
        positions=(make_position("000"),),
        last_exit_at={"000": NOW - timedelta(days=1)},
    )

    result = apply(Target((entry("000"),)), ctx, GateConfig(cooldown_days=5))

    assert len(result.target) == 1


# --- 잡다한 경계 --------------------------------------------------------------


def test_zero_weight_for_an_unheld_symbol_is_dropped_silently():
    ctx = make_ctx()

    result = apply(Target((exit_item("100"),)), ctx)

    assert len(result.target) == 0
    assert result.rejections == ()


def test_rejection_order_follows_input_order():
    held = tuple(make_position(f"00{i}") for i in range(5))
    ctx = make_ctx(positions=held, used_event_ids=frozenset({"E2"}))
    target = Target(
        (
            *[entry(p.symbol) for p in held],
            entry("100", event_id="E1"),
            entry("101", event_id="E2"),
        )
    )

    result = apply(target, ctx)

    assert [(r.symbol, r.reason) for r in result.rejections] == [
        ("100", RejectReason.SLOT_FULL),
        ("101", RejectReason.DUPLICATE_EVENT),
    ]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_positions": 0},
        {"max_weight": 0.0},
        {"max_weight": 1.5},
        {"cooldown_days": -1},
    ],
)
def test_invalid_config_is_rejected(kwargs):
    with pytest.raises(ValueError):
        GateConfig(**kwargs)


def test_cooldown_days_one_is_exactly_the_same_day_reentry_block():
    """**R14는 새 손잡이가 필요 없다.**

    분봉에서 "같은 날 1회"를 표현할 수 없다는 것이 R14의 전제였는데,
    `cooldown_days=1`이 정확히 그것이다 — `(now.date() - last_exit.date()).days
    < 1`이므로 청산한 날의 나머지 사이클이 전부 막히고 다음 거래일에 풀린다.

    봉 단위 손잡이를 따로 두면 같은 것을 두 번 표현하게 되고, 어느 쪽이
    이기는지를 나중에 되짚어야 한다.
    """
    exited = NOW.replace(hour=10, minute=0)

    same_day = make_ctx(now=exited.replace(hour=15, minute=19), last_exit_at={"100": exited})
    blocked = apply(Target((entry("100"),)), same_day, GateConfig(cooldown_days=1))
    assert len(blocked.target) == 0
    assert blocked.rejections[0].reason is RejectReason.COOLDOWN

    next_day = make_ctx(
        now=(exited + timedelta(days=1)).replace(hour=9, minute=0), last_exit_at={"100": exited}
    )
    assert len(apply(Target((entry("100"),)), next_day, GateConfig(cooldown_days=1)).target) == 1
