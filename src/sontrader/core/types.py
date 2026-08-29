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


class ExitReason(str, Enum):
    """왜 팔았는가. **`core/exit_rules.py`가 판정하지만 타입은 여기 둔다** —
    `Order`가 이 값을 싣고 다녀야 하는데(리서처 R16), 타입이 exit_rules에
    있으면 types ← exit_rules ← types 순환이 된다.

    사유를 주문에 실어 보내는 이유는 `exit_rule`과 같다 — 실전은 체결에 시차가
    있어서, 체결을 확인하는 사이클에는 판정 근거가 이미 사라져 있다. 사후에
    다시 계산하면 그때의 봉으로 재판정하게 되어 원래 사유와 갈릴 수 있다.
    """

    STOP = "stop"  # 스톱 레벨 이탈 (고정 손절 / 본전 / ATR 트레일링)
    MAX_HOLD = "max_hold"  # 최대 보유기간 상한 (달력일 또는 봉 개수)
    EOD = "eod"  # 세션 종료 임박 — 오버나이트 금지 (데이트레이딩)


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


class StopBasis(str, Enum):
    """스톱 이탈을 **봉의 어느 가격으로** 판정할지 (설계 4절의 미해결 지점).

    설계는 *"트레일링 판정은 **분봉** 종가 기준, 틱 저가로 판정하지 않는다
    (장중 스파이크 노이즈 회피)"* 라고 정했다. 그런데 우리는 분봉이 아니라
    **일봉**으로 판정한다 — 그러면 "종가 기준"의 의미가 완전히 달라진다.
    분봉 종가는 사실상 연속 감시지만, 일봉 종가는 **하루에 한 번**이다.

    둘 다 근사이고 **틀리는 방향이 서로 반대다**:

    | | 놓치는 것 | 결과 |
    |---|---|---|
    | `CLOSE` | 장중에 스톱을 깨고 종가에 회복한 날 | 스톱이 발동하지 않아 손실이 커진 뒤 나간다 |
    | `LOW` | 스파이크로 한 번 스친 것도 이탈로 본다 | 설계가 피하려던 노이즈 청산 |

    실측(2026-08-26, quant researcher): 손실 769건 중 525건(68.3%)이 −5%
    스톱보다 나쁘게 청산됐고 평균 −9.13%다. 갭은 실현손실의 5.6%뿐이라
    원인은 갭이 아니라 **판정 주기**다.

    어느 쪽을 쓸지는 백테스트로 정한다(설계 8절). 그래서 값이 아니라
    **선택지**를 코드에 둔다.
    """

    CLOSE = "close"  # 봉 종가로 판정 (현행 기본값 — 설계 문구를 일봉에 직역한 것)
    LOW = "low"  # 봉 저가로 판정 — 장중에 스톱을 건드리면 이탈


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

    `atr_period`/`atr_k`/`breakeven_trigger`는 LLM 출력이 아니라 시스템
    파라미터(설계 8절, 백테스트로 확정)지만 포지션마다 함께 저장한다. 전역 값이
    나중에 바뀌어도 과거 포지션의 스톱 레벨이 그대로 재현되어야 하기 때문이다.
    """

    technical: TechnicalExit = TechnicalExit.ATR_TRAILING
    max_hold_days: int = 30
    # 보유 상한을 **봉 개수**로도 건다 (T24 선택지 B / 리서처 R13). None이면 미사용.
    #
    # `max_hold_days`와 함께 적용되고 **먼저 걸리는 쪽이 이긴다.** 데이트레이딩은
    # 하루 안에 닫아야 하는데 달력일 최소 단위가 1이라 그것으로는 표현할 수
    # 없었다 — `max_hold_days=1`은 "다음 날 첫 사이클"이고 0은 거부된다.
    #
    # **왜 시각이 아니라 개수인가**: core는 장 마감 시각을 알지 않는다(구조 원칙
    # 1). 시각을 넣으면 "15:20"이 상수로 박히는데, 실측 251거래일 중 11일이
    # 09:00~15:30이 아니다(`docs/system/02-매매-정교화.md` T23). 개수로 두면
    # 정지일에 자동으로 느슨해지고, 그게 T23 규약과 방향이 같다.
    #
    # **값은 백테스트가 정한다** (01문서 §8). 기본 None = 스윙 동작 그대로.
    max_hold_bars: int | None = None
    # 세션 종료 N봉 전에 청산 신호를 낸다 (`ExitReason.EOD` / 리서처 R12).
    # None이면 미사용. 남은 봉 수는 **주입된다**(`Context.session_bars_remaining`)
    # — core가 세션 경계를 계산하지 않는다는 뜻이다.
    #
    # **값은 백테스트가 정한다.** 리서처 설계는 20봉을 예시로 들었지만 그것도
    # 확정값이 아니다 (04-ORB-설계 §8.1).
    eod_exit_bars: int | None = None
    stop_loss_pct: float = -0.05  # 진입가 대비 고정 손절 (음수)
    atr_period: int = 14  # 봉 개수. 봉 주기는 주입하는 쪽이 정한다
    atr_k: float = 2.0
    # 이 수익률에 도달하면 스톱을 본전 위로 올린다 (설계 4절 3구간 스톱의
    # 구간 전환점). 원래 exit_rules 모듈 상수였는데, 다른 스톱 파라미터가
    # 전부 여기 있어 혼자만 주입 불가였다 — 백테스트로 확정할 값이므로
    # 포지션에 함께 저장해야 과거 스톱이 재현된다.
    breakeven_trigger: float = 0.05
    # 스톱 이탈을 봉의 어느 가격으로 볼지 (`StopBasis` 참고). 다른 스톱
    # 파라미터와 같은 이유로 포지션에 함께 저장한다 — 전역 기본값이 나중에
    # 바뀌어도 과거 포지션의 판정이 그대로 재현돼야 한다.
    stop_basis: StopBasis = StopBasis.CLOSE

    def __post_init__(self) -> None:
        if self.max_hold_days <= 0:
            raise ValueError(f"max_hold_days must be positive: {self.max_hold_days}")
        if self.max_hold_bars is not None and self.max_hold_bars < 1:
            raise ValueError(f"max_hold_bars must be >= 1 or None: {self.max_hold_bars}")
        if self.eod_exit_bars is not None and self.eod_exit_bars < 0:
            raise ValueError(f"eod_exit_bars must be >= 0 or None: {self.eod_exit_bars}")
        if not -1.0 < self.stop_loss_pct < 0.0:
            raise ValueError(f"stop_loss_pct must be in (-1, 0): {self.stop_loss_pct}")
        if self.atr_period < 1:
            raise ValueError(f"atr_period must be >= 1: {self.atr_period}")
        if self.atr_k <= 0:
            raise ValueError(f"atr_k must be positive: {self.atr_k}")
        if self.breakeven_trigger <= 0.0:
            raise ValueError(f"breakeven_trigger must be positive: {self.breakeven_trigger}")

    def to_dict(self) -> dict[str, Any]:
        """`positions.exit_rule_json` 저장 형식. LLM의 한글 키 스키마와는 별개다."""
        return {
            "technical": self.technical.value,
            "max_hold_days": self.max_hold_days,
            "max_hold_bars": self.max_hold_bars,
            "eod_exit_bars": self.eod_exit_bars,
            "stop_loss_pct": self.stop_loss_pct,
            "atr_period": self.atr_period,
            "atr_k": self.atr_k,
            "breakeven_trigger": self.breakeven_trigger,
            "stop_basis": self.stop_basis.value,
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
        # 모르는 판정 기준도 거부한다(fail-closed) — 조용히 기본값으로
        # 대체하면 그 포지션만 다른 규칙으로 판정되고 아무도 모른다.
        raw_basis = payload.get("stop_basis", defaults.stop_basis.value)
        try:
            stop_basis = StopBasis(raw_basis)
        except ValueError as exc:
            raise ValueError(f"unknown stop basis: {raw_basis!r}") from exc
        return cls(
            technical=technical,
            max_hold_days=int(payload.get("max_hold_days", defaults.max_hold_days)),
            max_hold_bars=_optional_int(payload.get("max_hold_bars", defaults.max_hold_bars)),
            eod_exit_bars=_optional_int(payload.get("eod_exit_bars", defaults.eod_exit_bars)),
            stop_loss_pct=float(payload.get("stop_loss_pct", defaults.stop_loss_pct)),
            atr_period=int(payload.get("atr_period", defaults.atr_period)),
            atr_k=float(payload.get("atr_k", defaults.atr_k)),
            breakeven_trigger=float(payload.get("breakeven_trigger", defaults.breakeven_trigger)),
            stop_basis=stop_basis,
        )


def _optional_int(value: Any) -> int | None:
    """저장된 JSON의 `None`을 살려서 읽는다 — `int(None)`은 터진다."""
    return None if value is None else int(value)


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
    # 청산 항목(weight=0)에만 존재. 왜 팔기로 했는지를 주문까지 실어 보낸다
    # (R16) — 없으면 성과를 만든 것이 스톱인지 EOD인지 나중에 못 가른다.
    exit_reason: ExitReason | None = None

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

    `ts`는 이 주문을 만든 사이클의 시각이다 (`Context.now`). 체결 시뮬레이터
    (`adapters/broker_sim.py`, 5단계)가 "이 주문 다음 봉"을 찾는 기준이 된다 —
    신호가 발생한 봉의 가격으로 즉시 체결하면 look-ahead이므로, 체결은 항상
    이 시각 이후의 봉에서 일어나야 한다(01문서 §4.1).

    `exit_rule`은 **이 주문이 체결되면 붙일 청산 조건**이다(매수만; 매도는 None).
    주문이 들고 가야 하는 이유는 실전의 체결 시차 때문이다 — 08:35에 낸 주문이
    09:00 시초가에 체결되면, 체결을 확인하는 사이클의 목표 포트폴리오에는 그
    종목이 이미 없을 수 있다. 그러면 청산 조건을 복원할 방법이 사라지고,
    포지션이 스톱 없이 방치된다. 백테스트는 즉시 체결이라 이 문제를 겪지 않아
    `CycleResult.target`에서 꺼내 써 왔지만, 그 방식은 실전에서 성립하지 않는다.

    `exit_reason`은 **왜 파는가**다(매도만; 매수는 None). 같은 이유로 주문이
    들고 간다 — 체결을 확인하는 사이클에는 판정 근거가 된 봉이 이미 지나가
    있어서, 사후에 다시 계산하면 그때의 봉으로 재판정하게 되고 원래 사유와
    갈릴 수 있다. 이게 없으면 성과를 만든 것이 스톱인지 EOD인지 못 가른다
    (리서처 R16).
    """

    idempotency_key: str
    symbol: str
    side: Side
    qty: int
    order_type: OrderType
    urgency: Urgency
    ts: datetime
    limit_price: int | None = None
    event_id: str | None = None
    order_id: str | None = None
    exit_rule: ExitRule | None = None
    # 왜 파는가 (매도만). 위 docstring 참고 — 성과 분해(R16)의 유일한 근거다.
    exit_reason: ExitReason | None = None
    # 이 주문을 만들 때 본 가격 — 마지막 완성 봉의 종가(`core/diff.py`).
    # 집행에는 쓰이지 않는다(시장가다). 오직 **사후 측정**을 위해 실어 나른다:
    # 체결가 − 이 값 = 의사결정 시점부터 체결까지 실제로 잃은 가격이고, 그
    # 분포가 `SimBrokerConfig.slippage_bps`(현재 10bp 자리표시자)의 실측
    # 근거가 된다. 주문 시점에 남기지 않으면 나중에 복원할 방법이 없다 —
    # 그날의 종가는 알아도 "그 사이클이 본 마지막 봉"은 알 수 없다.
    # 봉이 없어도 청산은 나가므로(위 docstring) None일 수 있다.
    ref_price: int | None = None

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
    # 주문을 냈지만 체결이 아직 확인되지 않은 종목 (미체결).
    #
    # **`positions`와 함께 "현재 상태"를 이룬다.** 브로커 잔고에는 체결된 것만
    # 잡히므로, 이 집합이 없으면 주문 직후 사이클이 "아직 아무것도 없다"고
    # 판단해 같은 종목을 다시 산다. 실전에서 실제로 겪었다 — 08:35 매수 5건이
    # 미체결인 동안 08:39에 같은 5종목을 또 주문했다.
    #
    # 멱등 키로는 막지 못한다. 키에 사이클 시각이 들어가 있어 다른 사이클이면
    # 다른 키다 — 멱등 키는 "같은 사이클의 재전송"을 막지, "다음 사이클의 중복
    # 진입"을 막지 않는다.
    #
    # 백테스트는 체결이 즉시라 항상 비어 있다(`broker_sim`이 submit 안에서
    # 체결한다). 그래서 이 필드는 실전에서만 값이 찬다.
    pending_order_symbols: frozenset[str] = frozenset()
    # 종목 → 매매수량단위(`symbol_master.trading_unit`). 주문 수량이 이 값의
    # 배수가 아니면 **KIS가 주문을 거부**한다. 백테스트가 이걸 모르면 실전에서
    # 존재할 수 없는 주문을 체결시키므로, 수량을 정하는 유일한 지점인
    # `core/diff.py`가 여기서 읽는다.
    #
    # 비어 있거나 없는 종목은 1로 본다 — 마스터에 없는 종목(신규 상장 직후,
    # 과거 상장폐지) 때문에 주문이 통째로 사라지는 편보다, 압도적으로 흔한
    # 값으로 진행하는 편이 낫다. 2026-08-26 실측 4,386종목 전부 1이다.
    trading_units: Mapping[str, int] = field(default_factory=dict)
    # 종목 → 그날 스냅샷에 **저장된** 워치리스트 순위 (`watchlist_snapshots.rank`).
    #
    # `watchlist` 튜플의 위치로 순위를 유추하면 안 된다. 저장된 rank는
    # 모멘텀 전체 풀에서의 순위이고 히스테리시스(30/42)로 일부만 남으므로
    # **구멍이 뚫려 있다** — 실측 1,864일 중 1,793일이 1..N 연속이 아니다
    # (예: 37종목인데 최대 rank 42). 위치+1을 쓰면 그건 저장된 순위가 아니라
    # 재계산한 순위이고, 01문서 §5.2의 "재계산 금지"를 어긴다.
    watchlist_ranks: Mapping[str, int] = field(default_factory=dict)
    # 이 세션에 **남은 연속거래 봉 수** (`now` 이후, 마감 단일가 봉 제외).
    # `ExitRule.eod_exit_bars` 판정의 유일한 입력이다. None이면 모른다는 뜻이고
    # EOD 청산은 발동하지 않는다 — 일봉 재생과 세션 정보가 없는 실전 경로가 그렇다.
    #
    # **core가 세션 경계를 계산하지 않는 이유가 이 필드다.** 실측 251거래일 중
    # 11일이 09:00~15:30이 아니라(10:00 개장, 16:30 마감, 15:32 지연 단일가 등)
    # 시각을 박으면 4.4%의 날에서 깨진다. 그날의 실제 봉에서 유도한 값을
    # 어댑터가 넣어 준다 (`apps/backtest.py`의 `SessionShape`).
    session_bars_remaining: int | None = None

    def position(self, symbol: str) -> Position | None:
        for pos in self.positions:
            if pos.symbol == symbol:
                return pos
        return None

    def watchlist_rank(self, symbol: str) -> int | None:
        """저장된 워치리스트 순위. 모르면 None (위 `watchlist_ranks` 주석 참고)."""
        return self.watchlist_ranks.get(symbol)

    def trading_unit(self, symbol: str) -> int:
        """매매수량단위. 모르면 1 (위 `trading_units` 주석 참고).

        0·음수가 들어오면 1로 본다 — 마스터 파싱 실패(빈 칸 → 0)가 나눗셈을
        터뜨리거나 수량을 0으로 만들어 매매를 통째로 멈추는 경로를 막는다.
        """
        unit = self.trading_units.get(symbol, 1)
        return unit if unit >= 1 else 1

    @property
    def held_symbols(self) -> frozenset[str]:
        return frozenset(pos.symbol for pos in self.positions)
