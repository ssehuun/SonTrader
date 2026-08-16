"""유니버스 방어 필터 (구현 계획 3단계). 순수 함수.

kis_trading utils/universe_filters.py의 검증된 규칙을 그대로 옮겼다:
주권(ST)만, 시가총액규모 미분류 제외, 거래정지·정리매매·관리종목·저유동성·
SPAC·불성실공시 제외, 우선주 제외, 시장경고 종목 제외, 기준가 하한,
KOSPI는 영업이익 양수 요구(KOSDAQ은 성장주 특성상 미적용), 종목명 '스팩' 제외.

플래그는 마스터 파일의 원본 문자를 그대로 받는다 — 해석은 여기서만 한다.

## 필터가 둘로 나뉘는 이유

`symbol_master`는 **오늘자 스냅샷**이다. 그래서 필드를 두 부류로 갈라야 한다.

- **구조적 속성** (증권 종류, 우선주 여부, SPAC, 상장일자): 시간이 지나도
  변하지 않는다. ETF는 작년에도 ETF였고 우선주는 작년에도 우선주였다.
  과거 어느 시점에 대해서도 오늘 값이 그대로 참이므로, **수집 단계에서
  걸러도 편향이 생기지 않는다** → `is_collectable()`.
- **시변 상태** (관리종목, 거래정지, 시장경고, 영업이익, 시가총액규모,
  기준가): 오늘 관리종목인 회사가 작년엔 멀쩡했을 수 있다. 이걸 수집
  단계에서 걸면 백테스트가 "지금까지 살아남은 종목"만 보게 되어 성과가
  실제보다 좋게 나온다(생존 편향) → `is_tradeable()`, 유니버스 산출 시점에만.

두 함수를 나눠두면 "왜 안 모았나"와 "왜 안 샀나"가 코드에서 구분되고,
**편향이 들어올 수 있는 지점이 `is_tradeable()` 한 곳으로 고정된다.**
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

MIN_BASE_PRICE = 1000  # 동전주 제외 하한 (KRW)

# 모멘텀이 요구하는 이력은 `lookback + 1` = 253 거래일이다 — skip은 룩백 창
# 안쪽 시점이라 더해지지 않는다(`momentum.momentum_score` 참고). 거래일은 연
# 약 245일이므로 253거래일 ≈ 377 달력일이고, 여기에 거래정지·장기휴장 여유를
# 얹어 400으로 잡았다. 그보다 짧게 상장된 종목은 일봉을 모아봐야 유니버스
# 산출에서 점수 없이 탈락하므로 수집 대상에서 뺀다.
#
# 커밋 cb93994의 메시지는 이 값의 근거를 "252 + 21 = 273 거래일"로 적었는데
# 오류다. 상수값 자체는 253 기준으로도 안전해서 그대로 둔다.
MIN_LISTING_DAYS = 400

_FLAGGED = {"Y", "1", "TRUE", "T"}
_WARNING_CLEAR = {"", "0", "00"}


@dataclass(frozen=True)
class SecurityInfo:
    """symbol_master 한 행의 순수 표현 (core는 DB를 모른다)."""

    symbol: str
    name: str
    market: str  # KOSPI | KOSDAQ
    group_code: str
    cap_scale_code: str
    low_liquidity_yn: str
    spac_yn: str
    pref_share_code: str
    base_price: int | None
    suspended_yn: str
    liquidation_yn: str
    managed_yn: str
    market_warning_code: str
    unfaithful_yn: str
    op_profit: int | None


@dataclass(frozen=True)
class StructuralInfo:
    """수집 대상 판정에 쓰는 구조적 속성만 담은 표현.

    `SecurityInfo`와 겹치는 필드가 있지만 일부러 분리했다 — 이 타입에는
    시변 필드를 담을 수 없으므로, 수집 필터에 실수로 편향을 들여올 수 없다.
    """

    symbol: str
    name: str
    group_code: str
    pref_share_code: str
    spac_yn: str
    listing_date: date | None


def is_collectable(
    info: StructuralInfo, *, today: date, min_listing_days: int = MIN_LISTING_DAYS
) -> bool:
    """일봉을 모을 가치가 있는 종목인가 (구조적 판정만, 편향 없음).

    상장일자가 없으면(마스터 미수집·불량 값) 제외한다 — fail-closed.
    """
    if _norm(info.group_code) != "ST":
        return False
    if _norm(info.pref_share_code) != "0":
        return False
    if _norm(info.spac_yn) in _FLAGGED:
        return False
    if "스팩" in info.name:
        return False
    if info.listing_date is None:
        return False
    return (today - info.listing_date).days >= min_listing_days


def is_tradeable(info: SecurityInfo, *, min_base_price: int = MIN_BASE_PRICE) -> bool:
    # 핵심 안전 플래그가 NULL이면(마스터 미수집 행) 통과가 아니라 제외한다
    # — fail-closed. 정상 수집된 행은 항상 'Y'/'N' 같은 문자를 갖는다.
    if info.suspended_yn is None or info.liquidation_yn is None or info.managed_yn is None:
        return False
    if _norm(info.group_code) != "ST":
        return False
    if _norm(info.cap_scale_code) in ("", "0"):
        return False
    for flag in (
        info.suspended_yn,
        info.liquidation_yn,
        info.managed_yn,
        info.low_liquidity_yn,
        info.spac_yn,
        info.unfaithful_yn,
    ):
        if _norm(flag) in _FLAGGED:
            return False
    if _norm(info.pref_share_code) != "0":
        return False
    if _norm(info.market_warning_code) not in _WARNING_CLEAR:
        return False
    if info.base_price is None or info.base_price < min_base_price:
        return False
    if "스팩" in info.name:
        return False
    if info.market == "KOSPI" and (info.op_profit is None or info.op_profit <= 0):
        return False
    return True


def _norm(value: str | None) -> str:
    return (value or "").strip().upper()
