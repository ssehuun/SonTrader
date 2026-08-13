"""LLM 백엔드 프로토콜 — 특정 제공자에 묶이지 않는다 (구현 계획 6단계).

`llm/judge.py`가 하는 일(캐시 확인 → 미스면 호출 → 파싱·검증 → 캐시 저장)은
모델 제공자가 누구든 동일하다. 실제로 달라지는 부분은 "프롬프트를 보내고
텍스트 하나를 돌려받는" API 호출 방식뿐이다 — 그래서 그 한 조각만
`LLMBackend`로 떼어냈다. `adapters/broker.py`가 `Broker` 프로토콜 뒤에
`broker_kis`/`broker_sim`을 감추는 것과 같은 구조다.

캐시 키(`llm_judgments.model`)는 백엔드가 스스로 아는 `model_id`를 쓴다 —
`CachingJudge`가 별도로 모델 이름을 받지 않는 이유다. 그래야 "이 백엔드로
호출했는데 다른 모델 이름으로 캐시됨" 같은 설정 실수가 구조적으로 불가능하다.
"""

from __future__ import annotations

from typing import Protocol


class BackendError(RuntimeError):
    """모델 호출은 성공했지만(HTTP 200 등) 신뢰할 수 있는 응답을 얻지 못했다.

    거부(refusal)나 빈 응답처럼 "물어봤지만 답을 못 받은" 경우에 쓴다.
    네트워크·인증 실패는 각 백엔드가 쓰는 HTTP 클라이언트의 예외를 그대로
    전파한다(httpx.HTTPStatusError 등) — 여기서 감싸지 않는다.
    """


class LLMBackend(Protocol):
    model_id: str  # 캐시 키(Judgment.model)로 쓰인다

    def complete(self, *, system: str, user: str, schema: dict) -> str:
        """시스템 프롬프트 + 사용자 메시지로 모델을 호출해 원문 텍스트를 돌려준다.

        `schema`는 기대하는 JSON 스키마다. 백엔드가 구조화된 출력을
        지원하면(Anthropic 등) 그 기능으로 강제하고, 지원하지 않으면
        프롬프트에 설명으로 포함해 유도한다 — 어느 쪽이든 반환값은 "이
        스키마를 따르려고 한 원문 텍스트"이고, 실제 검증(닫힌 집합·범위
        확인)은 호출자(`llm/judge.py`)가 `core.types`의 데이터클래스로
        한다. 그래서 이 프로토콜은 스키마 강제 여부를 보장하지 않는다 —
        fail-closed 검증은 항상 judge.py 쪽에 있다.
        """
        ...
