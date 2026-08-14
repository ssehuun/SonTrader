"""data/positions.py 테스트 (구현 계획 5단계 잔여 작업)."""

from datetime import datetime

from sontrader.core.types import ExitRule, TechnicalExit
from sontrader.data import db, positions

NOW = datetime(2026, 3, 10, 9, 30)


def test_load_all_returns_empty_when_no_positions(db_engine):
    db.migrate(db_engine)
    assert positions.load_all(db_engine) == []


def test_load_all_reconstructs_exit_rule_from_json(db_engine):
    db.migrate(db_engine)
    exit_rule = ExitRule(
        technical=TechnicalExit.ATR_TRAILING, max_hold_days=20, stop_loss_pct=-0.07
    )
    with db_engine.begin() as conn:
        conn.execute(
            db.positions.insert().values(
                symbol="005930",
                qty=10,
                avg_price="71000.5000",
                entered_at=NOW,
                event_id=None,
                exit_rule_json=exit_rule.to_dict(),
            )
        )

    [record] = positions.load_all(db_engine)

    assert record.symbol == "005930"
    assert record.qty == 10
    assert record.avg_price == 71000.5
    assert record.entered_at == NOW
    assert record.exit_rule == exit_rule
    assert record.event_id is None
