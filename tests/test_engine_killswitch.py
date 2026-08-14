"""engine/killswitch.py 테스트 (구현 계획 6단계)."""

from datetime import datetime

from sontrader.data import db
from sontrader.engine import killswitch

NOW = datetime(2026, 3, 10, 9, 0)


def test_is_engaged_defaults_to_false(db_engine):
    db.migrate(db_engine)
    assert killswitch.is_engaged(db_engine) is False


def test_engage_sets_state_to_true(db_engine):
    db.migrate(db_engine)

    killswitch.engage(db_engine, now=NOW)

    assert killswitch.is_engaged(db_engine) is True


def test_disengage_after_engage_sets_state_back_to_false(db_engine):
    db.migrate(db_engine)
    killswitch.engage(db_engine, now=NOW)

    killswitch.disengage(db_engine, now=NOW)

    assert killswitch.is_engaged(db_engine) is False


def test_engage_is_idempotent_across_calls(db_engine):
    db.migrate(db_engine)

    killswitch.engage(db_engine, now=NOW)
    killswitch.engage(db_engine, now=NOW)

    assert killswitch.is_engaged(db_engine) is True
