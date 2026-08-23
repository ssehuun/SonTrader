"""LLM 판단 계층 — 진입 판단 + 청산조건 확정 (구현 계획 6단계).

01문서 §3: 진입 판단은 LLM이 하고, 청산 조건은 진입 시점에 확정해서 함께
출력한다. 판정 자체(스톱 발동 여부)는 그 이후 `core/exit_rules.py`가 결정적
으로 수행하며, 보유 중에는 LLM을 다시 부르지 않는다 — 그래야 (event_id,
prompt_version, model) 캐시가 성립하고 백테스트가 재현 가능해진다.

## 특정 제공자에 묶이지 않는다

이 모듈은 `LLMBackend`(`llm/backend.py`)를 주입받는다 — Claude든, OpenAI
호환 서버든, 나중에 추가될 다른 백엔드든 캐싱·파싱·검증 로직은 완전히
동일하다. "어느 모델을 쓸지"는 `apps/backtest.py`나 `cli.py`처럼 이
모듈을 호출하는 쪽이 결정한다.

## look-ahead 완화: 마스킹

프롬프트에는 종목코드·법인코드·공시 일시를 넣지 않는다 — 공시 유형과 제목만
전달한다. 그래도 제목 자체가 시점을 암시할 수 있어(예: "2026년 반기보고서")
완전히 제거되지는 않는다(01문서 §3.4) — 그래서 LLM 전략의 백테스트 성과는
상한선으로 간주하고 포워드 테스트를 우선해야 한다.

## 지금 채우지 못하는 입력

설계는 "공시 본문"을 판단 근거로 준다고 되어 있지만, 지금 DART 수집기
(`data/dart.py`)는 목록 API(list.json)만 수집해 본문을 갖고 있지 않다.
그래서 이 슬라이스는 제목·유형만으로 판단한다 — 신호가 약할 수 있다는
뜻이고, 본문 수집기는 별도 후속 작업이다. `judge()`가 `disclosure_text`를
별도 인자로 받는 이유가 이것이다: 나중에 본문 수집이 생겨도 이 함수
시그니처를 바꾸지 않고 더 풍부한 텍스트를 넘기기만 하면 된다.

## 캐시 우선

`CachingJudge.judge()`는 항상 먼저 캐시(`llm/cache.py`)를 확인한다 — 캐시
히트면 백엔드를 부르지 않는다. 같은 이벤트를 두 번 판단하면 비용이 들 뿐
아니라, 모델이 그 사이 업데이트됐을 경우 백테스트 재현성이 깨진다.

## 청산조건 파싱은 fail-closed

백엔드가 닫힌 집합(`TechnicalExit`) 밖의 값을 내거나 확신도·손절률 범위를
벗어나면(`core.types`의 `__post_init__`이 검사) 조용히 기본값으로 대체하지
않고 예외를 던진다 — 판정할 수 없는 청산조건을 가진 포지션은 스톱이 영영
발동하지 않기 때문이다(`ExitRule.from_dict`과 같은 원칙).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.engine import Engine

from sontrader.adapters.clock import Clock, RealClock
from sontrader.core.types import Event, ExitRule, Judgment, TechnicalExit
from sontrader.llm import cache
from sontrader.llm.backend import LLMBackend

PROMPT_VERSION = "v1"

_PROMPT_DIR = Path(__file__).parent / "prompts"

JudgeFn = Callable[[Event], Judgment | None]


def _load_prompt(version: str) -> str:
    return (_PROMPT_DIR / f"{version}.txt").read_text(encoding="utf-8")


def _response_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "진입": {"type": "boolean"},
            "확신도": {"type": "number"},
            "근거": {"type": "string"},
            "청산조건": {
                "type": "object",
                "properties": {
                    "기술적": {"type": "string", "enum": [e.value for e in TechnicalExit]},
                    "최대보유일": {"type": "integer"},
                    "손절률": {"type": "number"},
                },
                "required": ["기술적", "최대보유일", "손절률"],
                "additionalProperties": False,
            },
        },
        "required": ["진입", "확신도", "근거", "청산조건"],
        "additionalProperties": False,
    }


class CachingJudge:
    """캐시 우선으로 백엔드를 호출해 이벤트를 판단한다.

    02문서 §2.2의 "API 호출 + 캐시" 실전 구현. 어떤 `LLMBackend`를 주입받든
    동작은 같다 — 이 클래스는 특정 모델 제공자를 알지 않는다.
    """

    def __init__(
        self,
        engine: Engine,
        backend: LLMBackend,
        *,
        prompt_version: str = PROMPT_VERSION,
        clock: Clock | None = None,
    ) -> None:
        self._engine = engine
        self._backend = backend
        self._prompt_version = prompt_version
        self._prompt = _load_prompt(prompt_version)
        self._clock = clock or RealClock()

    def judge(self, event: Event, disclosure_text: str | None = None) -> Judgment:
        cached = cache.load(
            self._engine, event.event_id, self._prompt_version, self._backend.model_id
        )
        if cached is not None:
            return cached

        judgment = self._call(event, disclosure_text)
        cache.store(self._engine, judgment, created_at=self._clock.now())
        return judgment

    def _call(self, event: Event, disclosure_text: str | None) -> Judgment:
        text = disclosure_text or event.title
        user_message = f"공시 유형: {event.event_type}\n공시 제목: {text}"
        raw = self._backend.complete(
            system=self._prompt, user=user_message, schema=_response_schema()
        )

        payload = json.loads(raw)
        verdict = bool(payload["진입"])
        exit_rule: ExitRule | None = None
        if verdict:
            raw_exit = payload["청산조건"]
            exit_rule = ExitRule(
                technical=TechnicalExit(raw_exit["기술적"]),
                max_hold_days=int(raw_exit["최대보유일"]),
                stop_loss_pct=float(raw_exit["손절률"]),
            )
        return Judgment(
            event_id=event.event_id,
            prompt_version=self._prompt_version,
            model=self._backend.model_id,
            verdict=verdict,
            confidence=float(payload["확신도"]),
            exit_rule=exit_rule,
            rationale=str(payload["근거"]),
        )
