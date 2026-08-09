"""core 공용 타입 (구현 계획 4단계). 순수 데이터 클래스만 — 부작용 없음.

02-코드-구조.md §2가 지정한 `core/types.py`. 전략·게이트·diff·청산규칙이
주고받는 값들의 정의가 전부 여기 모인다. 표준 라이브러리 외에는 아무것도
import하지 않는다 (구조 원칙 1).

설계 대응:

- `Urgency`가 집행 비대칭을 코드로 표현한다 (§3.3): 청산은 IMMEDIATE(장중
  즉시), 진입은 NEXT_OPEN(다음 개장 시가).
- `Target`은 주문이 아니라 **목표 상태**다 (§2.4). 엔진이 (목표 − 현재)를
  계산해 주문을 만들므로 중복 주문이 원리적으로 불가능하다.
- `ExitRule.technical`은 자유 서술이 아니라 **사전 정의된 닫힌 집합**이다
  (§3.2). 자유 서술 청산은 코드로 판정할 수 없다.
- `Position`에 high_water가 없는 것은 의도적이다 (§6.5). 파생 데이터이므로
  진입 시각 이후 봉에서 재계산한다.
- `BarView`는 현재 시각 이후 봉 접근을 **구조적으로 차단**한다 (§3.2).
  여기서는 프로토콜만 정의하고 구현은 같은 단계의 `engine/context.py`에서 붙인다.

금액 단위는 원(KRW). 체결가·봉 가격은 정수, 가중평균 진입가와 스톱 레벨은
실수다 — 부분체결 평균가가 정수가 아니고, 이를 반올림하면 스톱 레벨이 미세
하게 흔들려 "하향 금지" 성질을 확인하기 어려워진다.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol


class Urgency(str, Enum):
    """집행 비대칭 (설계 1.3절). 이 두 값이 타이밍 정책의 전부다."""

    IMMEDIATE = "IMMEDIATE"  # 청산 — 장중 즉시 시장가
    NEXT_OPEN = "NEXT_OPEN"  # 진입 — 다음 개장 시가


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(str, Enum):
    """UNKNOWN은 "주문 접수 여부 불명" — API 타임아웃 시의 명시적 상태다.

    설계 2.6절이 요구하는 상태. 실패로 처리하면 중복 주문이 나가고, 성공으로
    처리하면 유령 포지션이 생긴다. 다음 사이클에서 주문 조회로 해소한다.
    """

    SUBMITTED = "submitted"
    UNKNOWN = "unknown"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class TechnicalExit(str, Enum):
    """사전 정의된 기술적 청산 규칙 (설계 3.2절 — LLM은 이 중에서 택1).

    닫힌 집합인 것이 요점이다. 값이 늘어나는 시점은 `core/exit_rules.py`에
    판정 코드가 추가되는 시점이며, 그 반대 순서는 없다. LLM 판단 계층에서
    프롬프트를 쓸 때 이 enum이 곧 LLM에 제시할 선택지가 된다.
    """

    # 고정 손절 → +5% 도달 시 본전 이동 → 이후 ATR 트레일링 (설계 4절)
    ATR_TRAILING = "atr_trailing"


@dataclass(frozen=True)
class Bar:
    """OHLCV 한 개. 주기(1분/1일)는 이 타입이 알지 않는다 — 주입하는 쪽의 몫."""

    symbol: str
    ts: datetime
    open: int
    high: int
    low: int
    close: int
    volume: int


@dataclass(frozen=True)
class Event:
    """공시 한 건 (`events` 테이블의 순수 표현).

    재생 기준 시각은 `ingested_at`이다 (설계 2.1절). `published_at`으로
    재생하면 실전에서는 수집 지연 때문에 잡을 수 없었던 기회를 잡아버린다.
    """

    event_id: str
    symbol: str | None
    corp_code: str
    event_type: str
    norm_key: str
    title: str
    published_at: datetime
    ingested_at: datetime


@dataclass(frozen=True)
class ExitRule:
    """진입 시점에 확정되는 청산 조건. 보유 중에는 바뀌지 않는다.

    설계 3.1절: LLM은 진입 시점에 청산 조건을 출력하고, 이후 판정은 규칙
    엔진이 결정적으로 수행한다. 보유 중 재호출하면 캐시가 무너지고 백테스트가
    비결정적이 된다.

    `atr_period`/`atr_k`는 LLM 출력이 아니라 시스템 파라미터(설계 8절, 백테스트로
    확정)지만 포지션마다 함께 저장한다. 전역 값이 나중에 바뀌어도 과거 포지션의
    스톱 레벨이 그대로 재현되어야 하기 때문이다.
    """

    technical: TechnicalExit = TechnicalExit.ATR_TRAILING
    max_hold_days: int = 30
    stop_loss_pct: float = -0.05  # 진입가 대비 고정 손절 (음수)
    atr_period: int = 14  # 봉 개수. 봉 주기는 주입하는 쪽이 정한다
    atr_k: float = 2.0

    def __post_init__(self) -> None:
        if self.max_hold_days <= 0:
            raise ValueError(f"max_hold_days must be positive: {self.max_hold_days}")
        if not -1.0 < self.stop_loss_pct < 0.0:
            raise ValueError(f"stop_loss_pct must be in (-1, 0): {self.stop_loss_pct}")
        if self.atr_period < 1:
            raise ValueError(f"atr_period must be >= 1: {self.atr_period}")
        if self.atr_k <= 0:
            raise ValueError(f"atr_k must be positive: {self.atr_k}")

    def to_dict(self) -> dict[str, Any]:
        """`positions.exit_rule_json` 저장 형식. LLM의 한글 키 스키마와는 별개다."""
        return {
            "technical": self.technical.value,
            "max_hold_days": self.max_hold_days,
            "stop_loss_pct": self.stop_loss_pct,
            "atr_period": self.atr_period,
            "atr_k": self.atr_k,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ExitRule:
        """저장 형식 → ExitRule. 모르는 technical 값은 거부한다 (fail-closed).

        판정할 수 없는 청산 조건을 가진 포지션은 스톱이 영영 발동하지 않으므로,
        조용히 기본값으로 대체하지 않고 명시적으로 실패시킨다.
        """
        raw = payload.get("technical", TechnicalExit.ATR_TRAILING.value)
        try:
            technical = TechnicalExit(raw)
        except ValueError as exc:
            raise ValueError(f"unknown technical exit rule: {raw!r}") from exc
        defaults = cls()
        return cls(
            technical=technical,
            max_hold_days=int(payload.get("max_hold_days", defaults.max_hold_days)),
            stop_loss_pct=float(payload.get("stop_loss_pct", defaults.stop_loss_pct)),
            atr_period=int(payload.get("atr_period", defaults.atr_period)),
            atr_k=float(payload.get("atr_k", defaults.atr_k)),
        )


@dataclass(frozen=True)
class Position:
    """보유 종목. high_water는 저장하지 않는다 (설계 6.5절 — 봉에서 재계산)."""

    symbol: str
    qty: int
    avg_price: float  # 부분체결 가중평균 진입가 (설계 2.6절)
    entered_at: datetime
    exit_rule: ExitRule
    event_id: str | None = None  # 동일 이벤트 재진입 차단의 근거

    def __post_init__(self) -> None:
        if self.qty <= 0:
            raise ValueError(f"position qty must be positive: {self.qty}")
        if self.avg_price <= 0:
            raise ValueError(f"avg_price must be positive: {self.avg_price}")


@dataclass(frozen=True)
class TargetItem:
    """목표 포트폴리오의 한 항목 (02 문서 §3.3)."""

    symbol: str
    weight: float
    urgency: Urgency
    exit_rule: ExitRule | None = None  # 신규 진입에만 존재
    event_id: str | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.weight <= 1.0:
            raise ValueError(f"weight must be in [0, 1]: {self.weight}")


@dataclass(frozen=True)
class Target:
    """전략이 반환하는 목표 상태. 주문이 아니다 (설계 2.4절).

    목표에 없는 보유 종목은 "전량 청산"을 의미한다. 비중 0짜리 항목을 넣는
    것과 같은 뜻이지만, 청산은 urgency=IMMEDIATE를 실어야 하므로 명시적으로
    항목을 남기는 쪽을 택한다 — diff가 긴급도를 알아야 한다.
    """

    items: tuple[TargetItem, ...] = ()

    def __post_init__(self) -> None:
        symbols = [item.symbol for item in self.items]
        if len(symbols) != len(set(symbols)):
            raise ValueError(f"duplicate symbols in target: {symbols}")

    def __iter__(self) -> Iterator[TargetItem]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def get(self, symbol: str) -> TargetItem | None:
        for item in self.items:
            if item.symbol == symbol:
                return item
        return None

    @property
    def symbols(self) -> frozenset[str]:
        return frozenset(item.symbol for item in self.items)


@dataclass(frozen=True)
class Order:
    """집행 의도. `order_id`는 엔진이 영속화할 때 부여한다.

    `idempotency_key`가 중복 주문 차단의 1차 방어선이고, DB의 UNIQUE 제약이
    2차 방어선이다 (설계 2.6절).
    """

    idempotency_key: str
    symbol: str
    side: Side
    qty: int
    order_type: OrderType
    urgency: Urgency
    limit_price: int | None = None
    event_id: str | None = None
    order_id: str | None = None

    def __post_init__(self) -> None:
        if self.qty <= 0:
            raise ValueError(f"order qty must be positive: {self.qty}")
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit order requires limit_price")


@dataclass(frozen=True)
class Fill:
    order_id: str
    price: int
    qty: int
    ts: datetime


@dataclass(frozen=True)
class Judgment:
    """LLM 판단 결과 (`llm_judgments` 한 행). 이벤트당 1회, 캐시 가능.

    core는 이 값을 **주입받기만 한다.** LLM 호출은 어댑터(`llm/judge.py`)의 일이며,
    전략이 직접 호출하면 순수성이 깨져 백테스트가 비결정적이 된다.
    """

    event_id: str
    prompt_version: str
    model: str
    verdict: bool  # 진입 여부
    confidence: float
    exit_rule: ExitRule | None
    rationale: str = ""

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0, 1]: {self.confidence}")
        if self.verdict and self.exit_rule is None:
            raise ValueError("a positive verdict must carry an exit rule")


class BarView(Protocol):
    """현재 시각까지의 봉만 노출하는 읽기 전용 뷰 (02 문서 §3.2).

    구현체는 `Context.now` 이후의 봉을 **반환하지 않는다.** 규율이 아니라
    접근 자체를 막는 것이 요점이다 — look-ahead는 백테스트를 통째로 거짓으로
    만든다. 구현과 미래 접근 시 예외 테스트는 `engine/context.py`에서 붙인다.
    """

    def history(self, symbol: str, count: int) -> Sequence[Bar]:
        """가장 최근 `count`개 봉을 시각 오름차순으로 반환 (부족하면 있는 만큼)."""
        ...

    def latest(self, symbol: str) -> Bar | None:
        """마지막 완성 봉. 미완성 봉은 노출하지 않는다."""
        ...


@dataclass(frozen=True)
class Context:
    """전략에 주입되는 읽기 전용 스냅샷 (02 문서 §3.2).

    타입은 core에 두고, DB에서 조립하는 코드는 `engine/context.py`에 둔다.
    그래야 core가 어떤 저장소도 알지 않은 채로 남는다.
    """

    now: datetime
    bars: BarView
    watchlist: tuple[str, ...]  # 그날 저장된 스냅샷 (재계산 금지)
    positions: tuple[Position, ...] = ()
    new_events: tuple[Event, ...] = ()  # ingested_at <= now 인 미처리 이벤트
    judgments: Mapping[str, Judgment] = field(default_factory=dict)  # event_id → 판단
    cash: int = 0
    equity: int = 0  # 총 평가금액 — 목표 비중을 수량으로 환산하는 기준
    # 이미 진입에 쓴 이벤트. 청산된 포지션까지 포함해야 동일 이벤트 재진입을
    # 막을 수 있다 (설계 2.5절) — 보유분만 보면 청산 직후 재진입이 뚫린다.
    used_event_ids: frozenset[str] = frozenset()
    # 종목 → 마지막 청산 시각. 게이트의 시간 기반 쿨다운 판정 근거.
    last_exit_at: Mapping[str, datetime] = field(default_factory=dict)

    def position(self, symbol: str) -> Position | None:
        for pos in self.positions:
            if pos.symbol == symbol:
                return pos
        return None

    @property
    def held_symbols(self) -> frozenset[str]:
        return frozenset(pos.symbol for pos in self.positions)
