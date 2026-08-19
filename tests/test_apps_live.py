"""apps/live.py 테스트 — 순수/DB 전용 헬퍼만 다룬다.

`main()` 자체(실제 KIS·텔레그램·웹소켓을 조립하는 진입점)는 cli.py의
다른 진입점들과 마찬가지로 단위 테스트 대상이 아니다 — 여기서는 그
안에서 쓰이는, 부작용이 좁게 격리된 조각들만 검증한다.
"""

from datetime import date

from sontrader.apps.live import (
    _build_judge,
    _build_notifier,
    _halt,
    _load_watchlist,
    _poll_telegram,
)
from sontrader.data import db
from sontrader.engine.reconcile import PositionMismatch, ReconcileReport


class RecordingNotifier:
    def __init__(self):
        self.messages = []
        self.processed = []

    def send_message(self, text):
        self.messages.append(text)

    def get_updates(self, *, offset=None, timeout=0):
        return self._updates

    def process_update(self, update, *, now):
        self.processed.append(update)


def test_load_watchlist_returns_symbols_in_rank_order(db_engine):
    db.migrate(db_engine)
    with db_engine.begin() as conn:
        conn.execute(
            db.watchlist_snapshots.insert(),
            [
                {"date": date(2026, 3, 10), "symbol": "000660", "score": 0.5, "rank": 2},
                {"date": date(2026, 3, 10), "symbol": "005930", "score": 0.9, "rank": 1},
            ],
        )

    assert _load_watchlist(db_engine, date(2026, 3, 10)) == ("005930", "000660")


def test_load_watchlist_uses_most_recent_snapshot_on_or_before_today(db_engine):
    """워치리스트는 장 마감 후 어제 종가로 계산돼 date=어제로 저장된다 —
    오늘 장중에는 date=오늘인 행이 아직 없으므로 어제 것을 써야 한다."""
    db.migrate(db_engine)
    with db_engine.begin() as conn:
        conn.execute(
            db.watchlist_snapshots.insert(),
            [{"date": date(2026, 3, 9), "symbol": "005930", "score": 0.9, "rank": 1}],
        )

    assert _load_watchlist(db_engine, date(2026, 3, 10)) == ("005930",)


def test_load_watchlist_ignores_snapshots_after_today(db_engine):
    db.migrate(db_engine)
    with db_engine.begin() as conn:
        conn.execute(
            db.watchlist_snapshots.insert(),
            [{"date": date(2026, 3, 11), "symbol": "005930", "score": 0.9, "rank": 1}],
        )

    assert _load_watchlist(db_engine, date(2026, 3, 10)) == ()


def test_load_watchlist_returns_empty_tuple_when_no_snapshot(db_engine):
    db.migrate(db_engine)
    assert _load_watchlist(db_engine, date(2026, 3, 10)) == ()


def test_poll_telegram_processes_each_update_and_advances_offset():
    notifier = RecordingNotifier()
    notifier._updates = [{"update_id": 5, "message": {"text": "/status"}}]

    new_offset = _poll_telegram(notifier, None)

    assert new_offset == 6
    assert len(notifier.processed) == 1


def test_poll_telegram_returns_original_offset_when_no_updates():
    notifier = RecordingNotifier()
    notifier._updates = []

    assert _poll_telegram(notifier, 3) == 3


def test_halt_sends_a_message_describing_each_mismatch():
    notifier = RecordingNotifier()
    report = ReconcileReport(
        positions=(),
        mismatches=(PositionMismatch("005930", "broker_only", 10, None),),
        resolved_orders=(),
    )

    _halt(notifier, report)

    assert len(notifier.messages) == 1
    assert "005930" in notifier.messages[0]
    assert "broker_only" in notifier.messages[0]


def test_halt_does_not_crash_without_a_notifier():
    report = ReconcileReport(
        positions=(),
        mismatches=(PositionMismatch("005930", "db_only", None, 5),),
        resolved_orders=(),
    )

    _halt(None, report)  # 예외만 안 나면 통과


def test_build_notifier_returns_none_without_telegram_credentials(monkeypatch, db_engine):
    monkeypatch.setattr("sontrader.config.load_dotenv", lambda: None)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    assert _build_notifier(db_engine) is None


def test_build_notifier_builds_when_credentials_present(monkeypatch, db_engine):
    monkeypatch.setattr("sontrader.config.load_dotenv", lambda: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")

    assert _build_notifier(db_engine) is not None


def test_build_judge_returns_none_without_anthropic_key(monkeypatch, db_engine):
    monkeypatch.setattr("sontrader.config.load_dotenv", lambda: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    assert _build_judge(db_engine) is None


def test_build_judge_returns_a_callable_when_key_present(monkeypatch, db_engine):
    db.migrate(db_engine)
    monkeypatch.setattr("sontrader.config.load_dotenv", lambda: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    judge = _build_judge(db_engine)

    assert callable(judge)
