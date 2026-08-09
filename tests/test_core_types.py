"""core 공용 타입 테스트 — 불변 조건과 저장 형식 왕복 (구현 계획 4단계)."""

from datetime import datetime

import pytest

from sontrader.core.types import (
    ExitRule,
    Judgment,
    Order,
    OrderType,
    Position,
    Side,
    Target,
    TargetItem,
    TechnicalExit,
    Urgency,
)

ENTRY_TS = datetime(2026, 1, 5, 9, 30)


# --- ExitRule ---------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_hold_days": 0},
        {"max_hold_days": -1},
        {"stop_loss_pct": 0.0},  # 양수/0 손절률은 의미가 없다
        {"stop_loss_pct": 0.05},
        {"stop_loss_pct": -1.0},
        {"atr_period": 0},
        {"atr_k": 0.0},
        {"atr_k": -1.0},
    ],
)
def test_exit_rule_rejects_nonsense_parameters(overrides):
    with pytest.raises(ValueError):
        ExitRule(**overrides)


def test_exit_rule_roundtrips_through_storage_form():
    rule = ExitRule(max_hold_days=10, stop_loss_pct=-0.07, atr_period=20, atr_k=3.0)
    assert ExitRule.from_dict(rule.to_dict()) == rule


def test_exit_rule_from_dict_fills_defaults():
    assert ExitRule.from_dict({}) == ExitRule()


def test_exit_rule_from_dict_rejects_unknown_technical_rule():
    # 판정기가 없는 규칙을 기본값으로 대체하면 스톱이 영영 발동하지 않는다.
    with pytest.raises(ValueError, match="unknown technical exit rule"):
        ExitRule.from_dict({"technical": "moon_phase"})


def test_technical_exit_is_a_closed_set():
    assert TechnicalExit("atr_trailing") is TechnicalExit.ATR_TRAILING
    with pytest.raises(ValueError):
        TechnicalExit("whatever_the_llm_made_up")


# --- Position / Judgment ----------------------------------------------------


def make_position(**overrides) -> Position:
    base = dict(
        symbol="005930",
        qty=10,
        avg_price=71000.0,
        entered_at=ENTRY_TS,
        exit_rule=ExitRule(),
    )
    return Position(**{**base, **overrides})


@pytest.mark.parametrize("overrides", [{"qty": 0}, {"qty": -1}, {"avg_price": 0.0}])
def test_position_rejects_impossible_values(overrides):
    with pytest.raises(ValueError):
        make_position(**overrides)


def test_position_has_no_high_water_field():
    # 설계 6.5절: 파생 데이터라 저장하지 않는다. 봉에서 재계산한다.
    assert not hasattr(make_position(), "high_water")


def test_positive_judgment_must_carry_an_exit_rule():
    # 청산 조건 없이 진입하면 그 포지션은 판정 불가 상태로 남는다.
    with pytest.raises(ValueError, match="exit rule"):
        Judgment(
            event_id="20260105000001",
            prompt_version="v1",
            model="claude-opus-5",
            verdict=True,
            confidence=0.8,
            exit_rule=None,
        )


def test_negative_judgment_needs_no_exit_rule():
    judgment = Judgment(
        event_id="20260105000001",
        prompt_version="v1",
        model="claude-opus-5",
        verdict=False,
        confidence=0.2,
        exit_rule=None,
    )
    assert judgment.verdict is False


@pytest.mark.parametrize("confidence", [-0.1, 1.1])
def test_judgment_confidence_must_be_a_probability(confidence):
    with pytest.raises(ValueError):
        Judgment(
            event_id="e",
            prompt_version="v1",
            model="m",
            verdict=False,
            confidence=confidence,
            exit_rule=None,
        )


# --- Target -----------------------------------------------------------------


def test_target_rejects_duplicate_symbols():
    # 같은 종목에 목표가 둘이면 diff가 어느 쪽을 따를지 정의되지 않는다.
    with pytest.raises(ValueError, match="duplicate"):
        Target(
            (
                TargetItem("005930", 0.2, Urgency.NEXT_OPEN),
                TargetItem("005930", 0.0, Urgency.IMMEDIATE),
            )
        )


@pytest.mark.parametrize("weight", [-0.01, 1.01])
def test_target_item_weight_must_be_a_fraction(weight):
    with pytest.raises(ValueError):
        TargetItem("005930", weight, Urgency.NEXT_OPEN)


def test_target_lookup_and_symbols():
    target = Target(
        (
            TargetItem("005930", 0.2, Urgency.NEXT_OPEN),
            TargetItem("000660", 0.0, Urgency.IMMEDIATE),
        )
    )
    assert len(target) == 2
    assert target.get("005930").weight == 0.2
    assert target.get("035720") is None
    assert target.symbols == frozenset({"005930", "000660"})


# --- Order ------------------------------------------------------------------


def test_limit_order_requires_a_price():
    with pytest.raises(ValueError, match="limit_price"):
        Order(
            idempotency_key="k",
            symbol="005930",
            side=Side.BUY,
            qty=10,
            order_type=OrderType.LIMIT,
            urgency=Urgency.NEXT_OPEN,
        )


def test_order_qty_must_be_positive():
    with pytest.raises(ValueError):
        Order(
            idempotency_key="k",
            symbol="005930",
            side=Side.SELL,
            qty=0,
            order_type=OrderType.MARKET,
            urgency=Urgency.IMMEDIATE,
        )


def test_enums_serialize_as_their_string_values():
    # exit_rule_json / orders.urgency 등에 그대로 실린다.
    assert Urgency.IMMEDIATE.value == "IMMEDIATE"
    assert Side.BUY.value == "buy"
    assert OrderType.MARKET.value == "market"
