"""유니버스 방어 필터 (구현 계획 3단계). 순수 함수.

kis_trading utils/universe_filters.py의 검증된 규칙을 그대로 옮겼다:
주권(ST)만, 시가총액규모 미분류 제외, 거래정지·정리매매·관리종목·저유동성·
SPAC·불성실공시 제외, 우선주 제외, 시장경고 종목 제외, 기준가 하한,
KOSPI는 영업이익 양수 요구(KOSDAQ은 성장주 특성상 미적용), 종목명 '스팩' 제외.

플래그는 마스터 파일의 원본 문자를 그대로 받는다 — 해석은 여기서만 한다.
"""

from __future__ import annotations

from dataclasses import dataclass

MIN_BASE_PRICE = 1000  # 동전주 제외 하한 (KRW)

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
