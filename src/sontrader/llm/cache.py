"""(event_id, prompt_version, model) 키 캐시 — `llm_judgments` 테이블 (구현 계획 6단계).

이 모듈은 캐시의 DB 읽기/쓰기만 담당한다 — API 호출은 하지 않는다
(`llm/judge.py`의 몫). 어떤 판단이든 최초 1회만 기록되고 재사용된다:
`store()`는 ON CONFLICT DO NOTHING을 쓴다 — 같은 키가 이미 있으면 새 결과로
덮어쓰지 않는다. LLM 출력은 결정적이지 않을 수 있어서, "이벤트당 1회
호출"이라는 캐시 성립 조건(01문서 §3.1)을 지키려면 최초 응답을 영구히
고정해야 한다.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from sontrader.core.types import ExitRule, Judgment
from sontrader.data import db


def load(engine: Engine, event_id: str, prompt_version: str, model: str) -> Judgment | None:
    columns = db.llm_judgments.c
    with engine.connect() as conn:
        row = conn.execute(
            sa.select(
                columns.verdict, columns.confidence, columns.exit_rule_json, columns.rationale
            ).where(
                columns.event_id == event_id,
                columns.prompt_version == prompt_version,
                columns.model == model,
            )
        ).first()
    if row is None:
        return None
    exit_rule = ExitRule.from_dict(row.exit_rule_json) if row.exit_rule_json else None
    return Judgment(
        event_id=event_id,
        prompt_version=prompt_version,
        model=model,
        verdict=row.verdict,
        confidence=row.confidence,
        exit_rule=exit_rule,
        rationale=row.rationale or "",
    )


def store(engine: Engine, judgment: Judgment, *, created_at: datetime) -> None:
    row = {
        "event_id": judgment.event_id,
        "prompt_version": judgment.prompt_version,
        "model": judgment.model,
        "verdict": judgment.verdict,
        "confidence": judgment.confidence,
        "exit_rule_json": judgment.exit_rule.to_dict() if judgment.exit_rule is not None else None,
        "rationale": judgment.rationale,
        "created_at": created_at,
    }
    with engine.begin() as conn:
        db.upsert_rows(
            conn,
            db.llm_judgments,
            [row],
            key_cols=("event_id", "prompt_version", "model"),
            ignore_conflicts=True,
        )
