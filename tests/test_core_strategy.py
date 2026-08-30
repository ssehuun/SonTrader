"""전략(build_target) 테스트 (구현 계획 4단계).

세 갈래로 나눠 확인한다: (1) 보유 종목의 청산 판정과 목표 비중, (2) 신규 진입
후보의 필터링·확신도 순위, (3) 두 흐름이 만날 때 종목 단위로만 경쟁한다는 것.
"""

from datetime import datetime, timedelta

import pytest

from sontrader.core.strategy import EntryTrigger, StrategyConfig, build_target
from sontrader.core.types import (
    Bar,
    Context,
    Event,
    ExitRule,
    Judgment,
    Position,
    Urgency,
)

SYMBOL = "005930"
ENTRY_TS = datetime(2026, 3, 1, 0, 0)
NOW = datetime(2026, 3, 5, 9, 30)


class StubBars:
    """symbol → 봉 시계열. history()와 latest()가 같은 데이터를 공유한다."""

    def __init__(self, series: dict[str, list[Bar]] | None = None) -> None:
        self._series = series or {}

    def history(self, symbol: str, count: int) -> list[Bar]:
        return self._series.get(symbol, [])[-count:]

    def latest(self, symbol: str) -> Bar | None:
        bars = self._series.get(symbol, [])
        return bars[-1] if bars else None


def make_bars(symbol: str, closes: list[int], start: datetime = ENTRY_TS) -> list[Bar]:
    return [
        Bar(
            symbol=symbol,
            ts=start + timedelta(days=i),
            open=c,
            high=c,
            low=c,
            close=c,
            volume=1_000,
        )
        for i, c in enumerate(closes)
    ]


def make_position(
    symbol: str = SYMBOL, *, qty: int = 100, rule: ExitRule | None = None, **overrides
) -> Position:
    base = dict(
        symbol=symbol,
        qty=qty,
        avg_price=10_000.0,
        entered_at=ENTRY_TS,
        exit_rule=rule or ExitRule(),
    )
    return Position(**{**base, **overrides})


def make_event(event_id: str, symbol: str | None) -> Event:
    return Event(
        event_id=event_id,
        symbol=symbol,
        corp_code="00000001",
        event_type="earnings",
        norm_key=f"key:{event_id}",
        title="공시",
        published_at=NOW,
        ingested_at=NOW,
    )


def make_judgment(
    event_id: str, *, verdict: bool = True, confidence: float = 0.8, rule: ExitRule | None = None
) -> Judgment:
    return Judgment(
        event_id=event_id,
        prompt_version="v1",
        model="test-model",
        verdict=verdict,
        confidence=confidence,
        exit_rule=rule or ExitRule() if verdict else None,
    )


def make_ctx(
    *,
    series: dict[str, list[Bar]] | None = None,
    positions: tuple[Position, ...] = (),
    watchlist: tuple[str, ...] = (),
    new_events: tuple[Event, ...] = (),
    judgments: dict[str, Judgment] | None = None,
    equity: int = 10_000_000,
    now: datetime = NOW,
    watchlist_ranks: dict[str, int] | None = None,
) -> Context:
    return Context(
        now=now,
        bars=StubBars(series),
        watchlist=watchlist,
        positions=positions,
        new_events=new_events,
        judgments=judgments or {},
        equity=equity,
        watchlist_ranks=watchlist_ranks or {},
    )


# --- 보유 종목: 청산 판정 -----------------------------------------------------


def test_stop_signal_liquidates_and_drops_exit_rule():
    position = make_position(event_id="E1")
    bars = make_bars(SYMBOL, [10_000, 9_000])  # 고정 -5% 스톱(9500) 이탈
    ctx = make_ctx(series={SYMBOL: bars}, positions=(position,))

    target = build_target(ctx)

    item = target.get(SYMBOL)
    assert item is not None
    assert item.weight == 0.0
    assert item.urgency is Urgency.IMMEDIATE
    assert item.exit_rule is None
    assert item.event_id == "E1"


def test_max_hold_signal_liquidates_even_without_bars():
    rule = ExitRule(max_hold_days=1)
    position = make_position(rule=rule)
    ctx = make_ctx(positions=(position,), now=ENTRY_TS + timedelta(days=2))

    item = build_target(ctx).get(SYMBOL)

    assert item is not None
    assert item.weight == 0.0
    assert item.urgency is Urgency.IMMEDIATE


# --- 보유 종목: 목표 비중은 현재 비중 -----------------------------------------


def test_held_position_targets_its_current_mark_to_market_weight():
    """오른 종목은 목표 비중으로 깎지 않는다 — ATR 트레일링과 충돌한다."""
    position = make_position(qty=250)  # 청산 신호 없음. 25.25% > entry_weight
    bars = make_bars(SYMBOL, [10_000, 10_100])  # 스톱 위, 종가 10,100
    ctx = make_ctx(series={SYMBOL: bars}, positions=(position,), equity=10_000_000)

    item = build_target(ctx).get(SYMBOL)

    assert item is not None
    assert item.weight == pytest.approx(250 * 10_100 / 10_000_000)
    assert item.weight > StrategyConfig().entry_weight
    assert item.urgency is Urgency.NEXT_OPEN


def test_an_underfilled_position_targets_the_entry_weight_again():
    """덜 채워진 진입은 되돌린다.

    현재 비중만 목표로 두면 증거금 상한에 걸려 덜 산 잔량을 채울 길이 없다 —
    S0(지수 매수보유)이 체결 1건으로 끝나 자본의 22.5%가 영구히 놀았다.
    """
    position = make_position(qty=100)  # 10.1% — entry_weight(19%) 아래
    bars = make_bars(SYMBOL, [10_000, 10_100])
    ctx = make_ctx(series={SYMBOL: bars}, positions=(position,), equity=10_000_000)

    item = build_target(ctx).get(SYMBOL)

    assert item is not None
    assert item.weight == pytest.approx(StrategyConfig().entry_weight)


def test_held_position_keeps_its_own_exit_rule_unchanged():
    rule = ExitRule(max_hold_days=7)
    position = make_position(rule=rule)
    bars = make_bars(SYMBOL, [10_000, 10_100])
    ctx = make_ctx(series={SYMBOL: bars}, positions=(position,))

    item = build_target(ctx).get(SYMBOL)

    assert item is not None
    assert item.exit_rule is rule


def test_weight_falls_back_to_entry_weight_without_a_price_bar():
    position = make_position()
    ctx = make_ctx(series={}, positions=(position,))  # 봉 없음 → history/latest 모두 빈 값

    item = build_target(ctx).get(SYMBOL)

    assert item is not None
    assert item.weight == pytest.approx(StrategyConfig().entry_weight)


def test_weight_falls_back_to_entry_weight_when_equity_is_unknown():
    position = make_position()
    bars = make_bars(SYMBOL, [10_000, 10_100])
    ctx = make_ctx(series={SYMBOL: bars}, positions=(position,), equity=0)

    item = build_target(ctx).get(SYMBOL)

    assert item is not None
    assert item.weight == pytest.approx(StrategyConfig().entry_weight)


# --- 신규 진입 후보 ------------------------------------------------------------


def test_watchlist_event_with_positive_verdict_becomes_an_entry():
    event = make_event("E1", "100")
    judgment = make_judgment("E1", confidence=0.9)
    ctx = make_ctx(watchlist=("100",), new_events=(event,), judgments={"E1": judgment})

    item = build_target(ctx).get("100")

    assert item is not None
    assert item.weight == pytest.approx(StrategyConfig().entry_weight)
    assert item.urgency is Urgency.NEXT_OPEN
    assert item.exit_rule is judgment.exit_rule
    assert item.event_id == "E1"


def test_event_outside_the_watchlist_is_ignored():
    event = make_event("E1", "100")
    ctx = make_ctx(watchlist=("999",), new_events=(event,), judgments={"E1": make_judgment("E1")})

    assert build_target(ctx).get("100") is None


def test_event_without_a_judgment_is_ignored():
    event = make_event("E1", "100")
    ctx = make_ctx(watchlist=("100",), new_events=(event,), judgments={})

    assert build_target(ctx).get("100") is None


def test_negative_verdict_is_ignored():
    event = make_event("E1", "100")
    judgment = make_judgment("E1", verdict=False)
    ctx = make_ctx(watchlist=("100",), new_events=(event,), judgments={"E1": judgment})

    assert build_target(ctx).get("100") is None


def test_event_for_a_symbol_that_is_not_stock_specific_is_ignored():
    # 비상장 관계사 공시 등 symbol=None인 이벤트.
    event = make_event("E1", None)
    ctx = make_ctx(watchlist=("100",), new_events=(event,), judgments={"E1": make_judgment("E1")})

    assert len(build_target(ctx)) == 0


# --- 보유·신규가 만날 때: 종목 단위 경쟁 --------------------------------------


def test_event_for_an_already_held_symbol_is_ignored():
    position = make_position(symbol="100")
    bars = make_bars("100", [10_000, 10_100])
    event = make_event("E1", "100")
    ctx = make_ctx(
        series={"100": bars},
        positions=(position,),
        watchlist=("100",),
        new_events=(event,),
        judgments={"E1": make_judgment("E1")},
    )

    target = build_target(ctx)

    assert len(target) == 1  # 보유분만 있고, 새 이벤트로 추가 진입하지 않는다
    assert target.get("100").event_id is None


def test_event_for_a_symbol_exiting_this_cycle_is_ignored():
    # 같은 종목이 이번 사이클에 청산되면서 동시에 재진입하는 것을 막는다.
    position = make_position(symbol="100", event_id="OLD")
    exit_bars = make_bars("100", [10_000, 9_000])
    event = make_event("NEW", "100")
    ctx = make_ctx(
        series={"100": exit_bars},
        positions=(position,),
        watchlist=("100",),
        new_events=(event,),
        judgments={"NEW": make_judgment("NEW")},
    )

    target = build_target(ctx)

    assert len(target) == 1
    item = target.get("100")
    assert item.weight == 0.0
    assert item.event_id == "OLD"


def test_multiple_events_for_one_symbol_keep_only_the_highest_confidence():
    low = make_event("LOW", "100")
    high = make_event("HIGH", "100")
    ctx = make_ctx(
        watchlist=("100",),
        new_events=(low, high),
        judgments={
            "LOW": make_judgment("LOW", confidence=0.3),
            "HIGH": make_judgment("HIGH", confidence=0.9),
        },
    )

    target = build_target(ctx)

    assert len(target) == 1
    assert target.get("100").event_id == "HIGH"


def test_entries_are_ranked_by_confidence_descending():
    events = (make_event("A", "100"), make_event("B", "200"), make_event("C", "300"))
    judgments = {
        "A": make_judgment("A", confidence=0.5),
        "B": make_judgment("B", confidence=0.9),
        "C": make_judgment("C", confidence=0.7),
    }
    ctx = make_ctx(watchlist=("100", "200", "300"), new_events=events, judgments=judgments)

    target = build_target(ctx)

    assert [item.symbol for item in target] == ["200", "300", "100"]


def test_confidence_ties_break_by_symbol_ascending():
    events = (make_event("A", "200"), make_event("B", "100"))
    judgments = {
        "A": make_judgment("A", confidence=0.5),
        "B": make_judgment("B", confidence=0.5),
    }
    ctx = make_ctx(watchlist=("100", "200"), new_events=events, judgments=judgments)

    target = build_target(ctx)

    assert [item.symbol for item in target] == ["100", "200"]


def test_held_items_precede_new_entries_in_the_result():
    position = make_position(symbol="000")
    bars = make_bars("000", [10_000, 10_100])
    event = make_event("E1", "100")
    ctx = make_ctx(
        series={"000": bars},
        positions=(position,),
        watchlist=("100",),
        new_events=(event,),
        judgments={"E1": make_judgment("E1")},
    )

    target = build_target(ctx)

    assert [item.symbol for item in target] == ["000", "100"]


# --- 결정성 -------------------------------------------------------------------


def test_same_inputs_produce_the_same_target():
    position = make_position(symbol="000")
    bars = make_bars("000", [10_000, 10_100])
    event = make_event("E1", "100")
    ctx = make_ctx(
        series={"000": bars},
        positions=(position,),
        watchlist=("100",),
        new_events=(event,),
        judgments={"E1": make_judgment("E1")},
    )

    assert build_target(ctx) == build_target(ctx)


# --- 설정값 검증 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [{"entry_weight": 0.0}, {"entry_weight": 1.5}, {"exit_history_bars": 0}],
)
def test_invalid_config_is_rejected(kwargs):
    with pytest.raises(ValueError):
        StrategyConfig(**kwargs)


# --- WATCHLIST_RANK 진입 트리거 ------------------------------------------------

WATCHLIST_CFG = StrategyConfig(entry_trigger=EntryTrigger.WATCHLIST_RANK)


def test_watchlist_mode_enters_without_any_event_or_judgment():
    """이벤트도 LLM 판단도 없이 진입 후보가 나온다 — 이 모드의 존재 이유다."""
    ctx = make_ctx(watchlist=("AAA", "BBB", "CCC"))

    target = build_target(ctx, WATCHLIST_CFG)

    assert [item.symbol for item in target] == ["AAA", "BBB", "CCC"]
    assert all(item.weight == WATCHLIST_CFG.entry_weight for item in target)


def test_watchlist_mode_preserves_rank_order():
    """게이트가 입력 순서를 슬롯 우선순위로 쓰므로 순위가 그대로 유지돼야 한다."""
    ctx = make_ctx(watchlist=("ZZZ", "AAA", "MMM"))

    target = build_target(ctx, WATCHLIST_CFG)

    assert [item.symbol for item in target] == ["ZZZ", "AAA", "MMM"]  # 종목코드순 아님


def test_watchlist_mode_skips_held_symbols():
    ctx = make_ctx(
        series={SYMBOL: make_bars(SYMBOL, [10_000] * 30)},
        positions=(make_position(SYMBOL),),
        watchlist=(SYMBOL, "AAA"),
    )

    target = build_target(ctx, WATCHLIST_CFG)

    entries = [item for item in target if item.symbol != SYMBOL]
    assert [item.symbol for item in entries] == ["AAA"]


def test_watchlist_mode_uses_default_exit_rule_and_no_event_id():
    """LLM이 청산조건을 주지 않으므로 기본 ExitRule로 통일한다."""
    ctx = make_ctx(watchlist=("AAA",))

    (item,) = build_target(ctx, WATCHLIST_CFG)

    assert item.event_id is None
    assert item.exit_rule == ExitRule()
    assert item.urgency is Urgency.NEXT_OPEN


def test_watchlist_mode_ignores_events_entirely():
    """이벤트가 있어도 워치리스트에 없으면 진입하지 않는다 — 트리거가 순위이지 이벤트가 아니다."""
    ctx = make_ctx(
        watchlist=("AAA",),
        new_events=(make_event("E1", "BBB"),),
        judgments={"E1": make_judgment("E1")},
    )

    target = build_target(ctx, WATCHLIST_CFG)

    assert [item.symbol for item in target] == ["AAA"]


def test_event_mode_remains_the_default():
    """기본값을 바꾸지 않았는지 — 기존 동작이 조용히 달라지면 안 된다."""
    assert StrategyConfig().entry_trigger is EntryTrigger.EVENT
    ctx = make_ctx(watchlist=("AAA",))
    assert list(build_target(ctx)) == []  # 이벤트 없으면 진입 없음


# --- exit_rule 주입 (파라미터 스윕 경로) ---------------------------------------


def test_watchlist_mode_uses_the_injected_exit_rule():
    """StrategyConfig.exit_rule이 신규 진입에 그대로 붙는다.

    이게 파라미터 스윕의 주입 지점이다 — 소스를 고치지 않고 스톱 폭을
    바꿔가며 백테스트를 돌릴 수 있어야 한다.
    """
    rule = ExitRule(stop_loss_pct=-0.12, atr_k=3.0, breakeven_trigger=0.15, max_hold_days=60)
    cfg = StrategyConfig(entry_trigger=EntryTrigger.WATCHLIST_RANK, exit_rule=rule)

    (item,) = build_target(make_ctx(watchlist=("AAA",)), cfg)

    assert item.exit_rule == rule


def test_default_exit_rule_is_unchanged():
    """기본값이 조용히 바뀌지 않았는지 — 기존 백테스트 결과가 달라지면 안 된다."""
    default = StrategyConfig().exit_rule
    assert default == ExitRule()
    assert default.stop_loss_pct == -0.05
    assert default.breakeven_trigger == 0.05
    assert default.atr_k == 2.0
    assert default.max_hold_days == 30


def test_event_mode_ignores_the_config_exit_rule():
    """EVENT 모드는 LLM이 종목별로 정한 규칙을 쓴다 — config가 덮어쓰면 안 된다."""
    llm_rule = ExitRule(stop_loss_pct=-0.20)
    cfg = StrategyConfig(exit_rule=ExitRule(stop_loss_pct=-0.03))
    ctx = make_ctx(
        watchlist=("AAA",),
        new_events=(make_event("E1", "AAA"),),
        judgments={"E1": make_judgment("E1", rule=llm_rule)},
    )

    (item,) = build_target(ctx, cfg)

    assert item.exit_rule == llm_rule
