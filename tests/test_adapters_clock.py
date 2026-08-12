"""Clock 어댑터 테스트 (구현 계획 5단계)."""

from datetime import datetime, timedelta, timezone

import pytest

from sontrader.adapters.clock import RealClock, ReplayClock

_KST = timezone(timedelta(hours=9))


def test_real_clock_returns_naive_kst_close_to_wall_clock():
    before = datetime.now(_KST).replace(tzinfo=None)
    now = RealClock().now()
    after = datetime.now(_KST).replace(tzinfo=None)

    assert now.tzinfo is None
    assert before <= now <= after


def test_replay_clock_starts_at_the_first_timestamp():
    timestamps = [datetime(2026, 3, 1), datetime(2026, 3, 2), datetime(2026, 3, 3)]

    assert ReplayClock(timestamps).now() == timestamps[0]


def test_replay_clock_advances_through_all_timestamps_in_order():
    timestamps = [datetime(2026, 3, 1), datetime(2026, 3, 2), datetime(2026, 3, 3)]
    clock = ReplayClock(timestamps)

    seen = [clock.now()]
    while clock.advance():
        seen.append(clock.now())

    assert seen == timestamps


def test_replay_clock_advance_returns_false_at_the_end_and_stays_put():
    clock = ReplayClock([datetime(2026, 3, 1), datetime(2026, 3, 2)])

    assert clock.advance() is True
    assert clock.advance() is False
    assert clock.advance() is False  # 반복 호출해도 계속 멈춰 있다
    assert clock.now() == datetime(2026, 3, 2)


def test_replay_clock_length_matches_the_timestamp_count():
    clock = ReplayClock([datetime(2026, 3, 1), datetime(2026, 3, 2), datetime(2026, 3, 3)])

    assert len(clock) == 3


def test_replay_clock_rejects_an_empty_sequence():
    with pytest.raises(ValueError):
        ReplayClock([])


def test_replay_clock_single_timestamp_never_advances():
    clock = ReplayClock([datetime(2026, 3, 1)])

    assert clock.advance() is False
    assert clock.now() == datetime(2026, 3, 1)
