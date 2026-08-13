"""CachingJudge / cached_only_judge 테스트 (구현 계획 6단계).

`LLMBackend`를 스텁으로 대체해 검증한다 — 캐싱·마스킹·fail-closed 파싱은
어떤 제공자를 쓰든 동일해야 하므로, 여기서는 특정 벤더 SDK를 흉내내지
않는다(그건 test_llm_anthropic_backend.py / test_llm_openai_backend.py의
몫). 중점적으로 보는 것: (1) 캐시 히트 시 백엔드를 다시 부르지 않는다,
(2) 프롬프트에 종목코드·법인코드·공시일시가 새지 않는다(마스킹),
(3) 스키마를 벗어난 값은 조용히 넘어가지 않고 예외로 막는다(fail-closed).
"""

import json
from datetime import datetime

import pytest

from sontrader.core.types import Event, ExitRule, Judgment
from sontrader.data import db
from sontrader.llm import cache
from sontrader.llm.backend import BackendError
from sontrader.llm.judge import CacheMissError, CachingJudge, cached_only_judge

NOW = datetime(2026, 3, 10, 9, 30)


class StubBackend:
    def __init__(self, respond, *, model_id: str = "stub-model"):
        self.model_id = model_id
        self._respond = respond
        self.calls: list[dict] = []

    def complete(self, *, system: str, user: str, schema: dict) -> str:
        self.calls.append({"system": system, "user": user, "schema": schema})
        return self._respond()


def make_payload(**overrides) -> dict:
    payload = {
        "진입": True,
        "확신도": 0.75,
        "근거": "실질적 실적 개선 근거",
        "청산조건": {"기술적": "atr_trailing", "최대보유일": 25, "손절률": -0.06},
    }
    payload.update(overrides)
    return payload


def make_backend(payload: dict, **kwargs) -> StubBackend:
    return StubBackend(lambda: json.dumps(payload, ensure_ascii=False), **kwargs)


def seed_event(engine, event_id: str = "E1") -> None:
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


def make_event(
    event_id: str = "E1",
    *,
    symbol: str = "005930",
    event_type: str = "earnings",
    title: str = "실적 개선 공시",
) -> Event:
    return Event(
        event_id=event_id,
        symbol=symbol,
        corp_code="00126380",
        event_type=event_type,
        norm_key=f"key:{event_id}",
        title=title,
        published_at=datetime(2026, 3, 1),
        ingested_at=datetime(2026, 3, 1),
    )


# --- 캐시 히트/미스 -------------------------------------------------------------


def test_judge_calls_the_backend_and_caches_result(db_engine):
    db.migrate(db_engine)
    seed_event(db_engine)
    backend = make_backend(make_payload())
    judge = CachingJudge(db_engine, backend)
    event = make_event()

    result = judge.judge(event)

    assert result.verdict is True
    assert result.confidence == pytest.approx(0.75)
    assert result.exit_rule.max_hold_days == 25
    assert result.exit_rule.stop_loss_pct == pytest.approx(-0.06)
    assert result.model == "stub-model"
    assert len(backend.calls) == 1

    again = judge.judge(event)

    assert again == result
    assert len(backend.calls) == 1  # 캐시 히트 — 백엔드 재호출 없음


def test_cache_is_populated_after_a_call(db_engine):
    db.migrate(db_engine)
    seed_event(db_engine)
    backend = make_backend(make_payload())
    judge = CachingJudge(db_engine, backend)

    result = judge.judge(make_event())

    assert cache.load(db_engine, "E1", "v1", "stub-model") == result


def test_different_backends_get_independent_cache_entries(db_engine):
    db.migrate(db_engine)
    seed_event(db_engine)
    a = CachingJudge(db_engine, make_backend(make_payload(확신도=0.9), model_id="model-a"))
    b = CachingJudge(db_engine, make_backend(make_payload(확신도=0.1), model_id="model-b"))
    event = make_event()

    result_a = a.judge(event)
    result_b = b.judge(event)

    assert result_a.confidence == pytest.approx(0.9)
    assert result_b.confidence == pytest.approx(0.1)


# --- 마스킹 ---------------------------------------------------------------------


def test_prompt_excludes_symbol_corp_code_and_publish_date(db_engine):
    db.migrate(db_engine)
    seed_event(db_engine)
    exit_condition = {"기술적": "atr_trailing", "최대보유일": 10, "손절률": -0.05}
    backend = make_backend(make_payload(진입=False, 청산조건=exit_condition))
    judge = CachingJudge(db_engine, backend)
    event = make_event(event_type="capital_change", title="유상증자 결정")

    judge.judge(event)

    sent = backend.calls[0]["user"]
    assert "유상증자 결정" in sent
    assert "capital_change" in sent
    assert "005930" not in sent  # 종목코드
    assert "00126380" not in sent  # 법인코드
    assert "2026" not in sent  # 공시 일시(연도)


# --- 판단 결과 --------------------------------------------------------------------


def test_negative_verdict_produces_no_exit_rule(db_engine):
    db.migrate(db_engine)
    seed_event(db_engine)
    backend = make_backend(
        make_payload(
            진입=False,
            확신도=0.3,
            근거="정형적 공시",
            청산조건={"기술적": "atr_trailing", "최대보유일": 10, "손절률": -0.05},
        )
    )
    judge = CachingJudge(db_engine, backend)

    result = judge.judge(make_event())

    assert result.verdict is False
    assert result.exit_rule is None


# --- fail-closed ------------------------------------------------------------------


def test_unknown_technical_exit_value_raises(db_engine):
    db.migrate(db_engine)
    seed_event(db_engine)
    backend = make_backend(
        make_payload(청산조건={"기술적": "trailing_stop_x", "최대보유일": 10, "손절률": -0.05})
    )
    judge = CachingJudge(db_engine, backend)

    with pytest.raises(ValueError):
        judge.judge(make_event())


def test_confidence_out_of_range_raises(db_engine):
    db.migrate(db_engine)
    seed_event(db_engine)
    backend = make_backend(make_payload(확신도=1.5))
    judge = CachingJudge(db_engine, backend)

    with pytest.raises(ValueError):
        judge.judge(make_event())


def test_backend_error_propagates(db_engine):
    db.migrate(db_engine)
    seed_event(db_engine)

    def raise_refusal():
        raise BackendError("model refused")

    backend = StubBackend(raise_refusal)
    judge = CachingJudge(db_engine, backend)

    with pytest.raises(BackendError):
        judge.judge(make_event())


# --- cached_only_judge (백테스트 모드) ---------------------------------------------


def test_cached_only_judge_returns_the_cached_judgment(db_engine):
    db.migrate(db_engine)
    seed_event(db_engine)
    judgment = Judgment(
        event_id="E1",
        prompt_version="v1",
        model="claude-opus-5",
        verdict=True,
        confidence=0.6,
        exit_rule=ExitRule(),
        rationale="근거",
    )
    cache.store(db_engine, judgment, created_at=NOW)
    judge = cached_only_judge(db_engine, model="claude-opus-5")

    assert judge(make_event()) == judgment


def test_cached_only_judge_raises_on_cache_miss(db_engine):
    db.migrate(db_engine)
    seed_event(db_engine)
    judge = cached_only_judge(db_engine, model="claude-opus-5")

    with pytest.raises(CacheMissError):
        judge(make_event())
