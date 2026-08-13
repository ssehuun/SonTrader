"""(event_id, prompt_version, model) 캐시 테스트 (구현 계획 6단계)."""

from datetime import datetime

from sontrader.core.types import ExitRule, Judgment, TechnicalExit
from sontrader.data import db
from sontrader.llm import cache

NOW = datetime(2026, 3, 10, 9, 30)


def seed_event(engine, event_id: str = "E1") -> None:
    # llm_judgments.event_id는 events.event_id를 참조하는 외래키다.
    with engine.begin() as conn:
        conn.execute(
            db.events.insert().values(
                event_id=event_id,
                symbol="005930",
                corp_code="00126380",
                event_type="earnings",
                norm_key=f"key:{event_id}",
                title="공시",
                published_at=NOW,
                ingested_at=NOW,
                raw_json={},
            )
        )


def make_judgment(
    event_id: str = "E1",
    *,
    prompt_version: str = "v1",
    model: str = "claude-opus-5",
    verdict: bool = True,
    confidence: float = 0.8,
    rule: ExitRule | None = None,
    rationale: str = "근거 문자열",
) -> Judgment:
    return Judgment(
        event_id=event_id,
        prompt_version=prompt_version,
        model=model,
        verdict=verdict,
        confidence=confidence,
        exit_rule=(rule or ExitRule()) if verdict else None,
        rationale=rationale,
    )


def test_load_returns_none_when_nothing_cached(db_engine):
    db.migrate(db_engine)

    assert cache.load(db_engine, "E1", "v1", "claude-opus-5") is None


def test_store_then_load_roundtrips_a_positive_judgment(db_engine):
    db.migrate(db_engine)
    seed_event(db_engine)
    rule = ExitRule(technical=TechnicalExit.ATR_TRAILING, max_hold_days=20, stop_loss_pct=-0.07)
    judgment = make_judgment(rule=rule, confidence=0.65, rationale="실적 개선 근거")

    cache.store(db_engine, judgment, created_at=NOW)
    loaded = cache.load(db_engine, "E1", "v1", "claude-opus-5")

    assert loaded == judgment


def test_store_then_load_roundtrips_a_negative_judgment(db_engine):
    db.migrate(db_engine)
    seed_event(db_engine)
    judgment = make_judgment(verdict=False, confidence=0.2, rationale="정형적 공시")

    cache.store(db_engine, judgment, created_at=NOW)
    loaded = cache.load(db_engine, "E1", "v1", "claude-opus-5")

    assert loaded == judgment
    assert loaded.exit_rule is None


def test_store_is_idempotent_first_write_wins(db_engine):
    # LLM 출력은 결정적이지 않을 수 있다 — 같은 키를 두 번 저장해도 최초
    # 결과가 캐시로 고정돼야 한다(01문서 §3.1의 "이벤트당 1회 호출" 전제).
    db.migrate(db_engine)
    seed_event(db_engine)
    first = make_judgment(confidence=0.9, rationale="첫 번째 응답")
    second = make_judgment(confidence=0.1, rationale="두 번째(다른) 응답")

    cache.store(db_engine, first, created_at=NOW)
    cache.store(db_engine, second, created_at=NOW)
    loaded = cache.load(db_engine, "E1", "v1", "claude-opus-5")

    assert loaded == first


def test_cache_is_scoped_by_prompt_version(db_engine):
    db.migrate(db_engine)
    seed_event(db_engine)
    v1 = make_judgment(prompt_version="v1", confidence=0.9)

    cache.store(db_engine, v1, created_at=NOW)

    assert cache.load(db_engine, "E1", "v1", "claude-opus-5") == v1
    assert cache.load(db_engine, "E1", "v2", "claude-opus-5") is None


def test_cache_is_scoped_by_model(db_engine):
    db.migrate(db_engine)
    seed_event(db_engine)
    opus = make_judgment(model="claude-opus-5", confidence=0.9)

    cache.store(db_engine, opus, created_at=NOW)

    assert cache.load(db_engine, "E1", "v1", "claude-opus-5") == opus
    assert cache.load(db_engine, "E1", "v1", "claude-sonnet-5") is None
