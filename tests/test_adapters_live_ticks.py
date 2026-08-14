"""adapters/live_ticks.py 테스트.

원시 메시지는 실시간-003(국내주식 실시간체결가) 문서의 Response Example을
그대로 쓴다(005930, 46개 필드) — 필드 순서·개수를 추측하지 않고 문서로
검증한다.
"""

from datetime import datetime

import pytest

from sontrader.adapters.live_ticks import MinuteBarAggregator, Tick, parse_tick_message

# 문서 "Response Example"의 # output 블록 그대로 (필드 46개).
_RECORD = (
    "005930^093354^71900^5^-100^-0.14^72023.83^72100^72400^71700^71900^71800^1^"
    "3052507^219853241700^5105^6937^1832^84.90^1366314^1159996^1^0.39^20.28^"
    "090020^5^-200^090820^5^-500^092619^2^200^20230612^20^N^65945^216924^"
    "1118750^2199206^0.05^2424142^125.92^0^^72100"
)


def test_parse_single_record_extracts_symbol_time_price_volume():
    raw = f"0|H0STCNT0|001|{_RECORD}"

    [tick] = parse_tick_message(raw)

    assert tick.symbol == "005930"
    assert tick.ts == datetime(2023, 6, 12, 9, 33, 54)
    assert tick.price == 71900
    assert tick.volume == 1


def test_parse_multiple_records_splits_by_field_count():
    raw = f"0|H0STCNT0|002|{_RECORD}^{_RECORD}"

    ticks = parse_tick_message(raw)

    assert len(ticks) == 2
    assert ticks[0] == ticks[1]


def test_parse_rejects_encrypted_messages():
    raw = f"1|H0STCNT0|001|{_RECORD}"

    with pytest.raises(NotImplementedError):
        parse_tick_message(raw)


def test_parse_rejects_unexpected_tr_id():
    raw = f"0|H0STOTHER|001|{_RECORD}"

    with pytest.raises(ValueError, match="tr_id"):
        parse_tick_message(raw)


def test_parse_rejects_field_count_mismatch():
    raw = "0|H0STCNT0|002|" + _RECORD  # count=2인데 필드는 1건뿐

    with pytest.raises(ValueError, match="expected"):
        parse_tick_message(raw)


def tick(symbol="005930", *, hh, mm, ss=0, price, volume=1) -> Tick:
    return Tick(symbol=symbol, ts=datetime(2026, 3, 10, hh, mm, ss), price=price, volume=volume)


def test_aggregator_does_not_emit_bar_for_first_tick_of_a_minute():
    agg = MinuteBarAggregator()

    result = agg.add(tick(hh=9, mm=30, price=71000))

    assert result is None


def test_aggregator_stays_silent_while_within_the_same_minute():
    agg = MinuteBarAggregator()
    agg.add(tick(hh=9, mm=30, ss=0, price=71000))

    result = agg.add(tick(hh=9, mm=30, ss=30, price=71500))

    assert result is None


def test_aggregator_emits_completed_bar_when_minute_rolls_over():
    agg = MinuteBarAggregator()
    agg.add(tick(hh=9, mm=30, ss=0, price=71000, volume=10))
    agg.add(tick(hh=9, mm=30, ss=20, price=71800, volume=5))
    agg.add(tick(hh=9, mm=30, ss=40, price=71200, volume=3))

    bar = agg.add(tick(hh=9, mm=31, ss=0, price=71300, volume=7))

    assert bar is not None
    assert bar.symbol == "005930"
    assert bar.ts == datetime(2026, 3, 10, 9, 30)
    assert bar.open == 71000
    assert bar.high == 71800
    assert bar.low == 71000
    assert bar.close == 71200
    assert bar.volume == 18  # 10+5+3, 다음 분 틱은 포함 안 됨


def test_aggregator_tracks_symbols_independently():
    agg = MinuteBarAggregator()
    agg.add(tick("005930", hh=9, mm=30, price=71000))
    agg.add(tick("000660", hh=9, mm=30, price=100000))

    bar_005930 = agg.add(tick("005930", hh=9, mm=31, price=71100))
    bar_000660 = agg.add(tick("000660", hh=9, mm=31, price=100500))

    assert bar_005930.symbol == "005930"
    assert bar_000660.symbol == "000660"


def test_aggregator_raises_on_out_of_order_tick():
    agg = MinuteBarAggregator()
    agg.add(tick(hh=9, mm=31, price=71000))

    with pytest.raises(ValueError, match="order"):
        agg.add(tick(hh=9, mm=30, price=70000))


def test_flush_returns_the_in_progress_bar():
    agg = MinuteBarAggregator()
    agg.add(tick(hh=9, mm=30, ss=0, price=71000, volume=10))
    agg.add(tick(hh=9, mm=30, ss=30, price=71200, volume=5))

    bar = agg.flush("005930")

    assert bar.ts == datetime(2026, 3, 10, 9, 30)
    assert bar.close == 71200
    assert bar.volume == 15


def test_flush_returns_none_when_nothing_pending():
    agg = MinuteBarAggregator()

    assert agg.flush("005930") is None


def test_flush_clears_state_so_the_next_tick_starts_fresh():
    agg = MinuteBarAggregator()
    agg.add(tick(hh=9, mm=30, price=71000))
    agg.flush("005930")

    # flush 이후 같은 분(9:30)에 새 틱이 와도 새로운 부분봉으로 취급된다
    # (out-of-order로 오인해 예외를 던지지 않는다).
    result = agg.add(tick(hh=9, mm=30, price=72000))

    assert result is None
