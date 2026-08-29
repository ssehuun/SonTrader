"""Environment-based configuration.

Credentials come from environment variables; a local ``.env`` file is
loaded automatically. See ``env.example`` for the full list.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv

REAL_BASE_URL = "https://openapi.koreainvestment.com:9443"
PAPER_BASE_URL = "https://openapivts.koreainvestment.com:29443"

_CACHE_DIR = Path.home() / ".cache" / "sontrader"

# 캐시 경로는 **모드별로 갈린다** (T31). 예전에는 파일 하나를 모의·실전이
# 함께 썼는데, 캐시가 발급 도메인을 함께 적으므로 모드가 다르면 매번 미스가
# 나고 재발급을 시도한다. 그리고 KIS는 **재발급 시 이전 토큰을 무효화**하고
# 발급 자체가 1분당 1회다.
#
# 실제로 겪었다 — 몇 시간짜리 실전 백필이 도는 중에 모의 명령을 돌리자
# 두 프로세스가 파일 하나를 서로 덮으며 `EGW00133`을 반복했다. 우회책
# (`SONTRADER_TOKEN_CACHE`를 손으로 분리)이 있었지만, **사람이 매번 기억해야
# 하는 규약은 이 저장소에서 반복해 실패했다** — 한 번 빠뜨리면 조용히
# 토큰을 뺏긴다. 그래서 기본값이 스스로 갈리게 한다.
DEFAULT_TOKEN_CACHE = _CACHE_DIR / "token.json"
DEFAULT_APPROVAL_KEY_CACHE = _CACHE_DIR / "approval_key.json"


def default_token_cache(paper: bool) -> Path:
    """모드별 기본 토큰 캐시 경로."""
    return _CACHE_DIR / f"token-{'paper' if paper else 'real'}.json"


def default_approval_key_cache(paper: bool) -> Path:
    """모드별 기본 접속키 캐시 경로 (토큰과 같은 이유)."""
    return _CACHE_DIR / f"approval_key-{'paper' if paper else 'real'}.json"


@dataclass(frozen=True)
class Settings:
    app_key: str
    app_secret: str
    cano: str  # 종합계좌번호: first 8 digits of the account number
    acnt_prdt_cd: str  # 계좌상품코드: 2 digits after the dash
    paper: bool = True
    token_cache: Path = field(default=DEFAULT_TOKEN_CACHE)
    approval_key_cache: Path = field(default=DEFAULT_APPROVAL_KEY_CACHE)

    @property
    def base_url(self) -> str:
        return PAPER_BASE_URL if self.paper else REAL_BASE_URL


def _optional_env(name: str) -> str | None:
    """Optional setting, kept separate from ``load_settings`` so commands
    only demand the credentials they actually use (DB-only commands don't
    need KIS keys and vice versa)."""
    load_dotenv()
    return os.environ.get(name) or None


def load_dart_api_key() -> str | None:
    """OpenDART API key (https://opendart.fss.or.kr)."""
    return _optional_env("DART_API_KEY")


def load_entry_trigger() -> str:
    """상시 가동의 신규 진입 트리거. ``watchlist``(기본) | ``event``.

    기본값이 ``watchlist``인 이유는 두 가지다. (a) LLM 없이도 매매가 돌아가야
    한다는 요건, (b) 백테스트로 실제 관통 검증을 마친 경로가 이쪽뿐이다 —
    이벤트 경로는 LLM 응답이 있어야 진입 후보가 생기고, 그 성과가 워치리스트
    단독보다 나은지 아직 비교되지 않았다.

    환경변수로 **명시**하게 둔 이유: ANTHROPIC_API_KEY 유무로 자동 전환하면
    키를 지우거나 만료시키는 것만으로 전략이 조용히 바뀐다. 실전에서 가장
    위험한 종류의 사고라 기동 로그에도 어느 쪽인지 찍는다.
    """
    value = (_optional_env("SONTRADER_ENTRY_TRIGGER") or "watchlist").strip().lower()
    if value not in ("watchlist", "event"):
        raise RuntimeError(f"SONTRADER_ENTRY_TRIGGER must be 'watchlist' or 'event', got {value!r}")
    return value


def load_anthropic_api_key() -> str | None:
    """Claude API key for the LLM 판단 계층 (6단계)."""
    return _optional_env("ANTHROPIC_API_KEY")


def load_openai_api_key() -> str | None:
    """OpenAI(호환) API key — LLM 판단 계층의 대체 백엔드 (6단계)."""
    return _optional_env("OPENAI_API_KEY")


def load_telegram_bot_token() -> str | None:
    """텔레그램 봇 토큰 — 승인 요청·알림·킬 스위치 (6단계). @BotFather로 발급."""
    return _optional_env("TELEGRAM_BOT_TOKEN")


def load_telegram_chat_id() -> str | None:
    """알림을 보낼 대상 채팅 ID (6단계). kis_trading 레포와 같은 변수명을 쓴다."""
    return _optional_env("TELEGRAM_CHAT_ID")


def load_database_url() -> str | None:
    """PostgreSQL URL for trading state.

    DATABASE_URL wins when set. Otherwise the URL is composed from the same
    POSTGRES_* variables the kis_trading collectors use, with the credentials
    URL-escaped — so passwords may contain @, :, / etc. without hand-encoding.
    """
    url = _optional_env("DATABASE_URL")
    if url:
        return url
    parts = {
        name: _optional_env(f"POSTGRES_{name}")
        for name in ("USER", "PASSWORD", "HOST", "PORT", "DB")
    }
    if not all(parts.values()):
        return None
    return (
        f"postgresql+psycopg2://{quote_plus(parts['USER'])}:{quote_plus(parts['PASSWORD'])}"
        f"@{parts['HOST']}:{parts['PORT']}/{parts['DB']}"
    )


def _load_credentials(paper: bool) -> tuple[str, str, str, str]:
    """환경에 맞는 자격증명 3종을 고른다.

    앱키는 환경별로 발급된다 — 모의 앱키를 실전 도메인에 쓰거나 그 반대면
    KIS가 `EGW02007: 해당 앱키는 모의투자용 앱키가 아닙니다`로 거절한다.
    그래서 두 벌을 각각 `KIS_APP_PAPER_*` / `KIS_APP_*`에 두고 `KIS_PAPER`로
    고른다. 한 벌만 쓰던 기존 `.env`도 그대로 동작하도록, 모의용 변수가
    없으면 접미사 없는 이름으로 넘어간다.

    조용한 실패를 막는 것이 핵심이다: 이름 하나 안 맞아서 실전 키가 모의
    도메인에 붙으면 시세조회는 통과하고 계좌 API만 막혀, 기동 후 한참 뒤에
    "왜 잔고가 안 보이지"로 드러난다.

    계좌번호는 어느 변수에서 왔는지까지 돌려준다 — 형식이 틀렸을 때 두 이름
    중 무엇을 고쳐야 하는지 오류 메시지가 짚어줘야 한다.
    """
    names = (
        ("KIS_APP_PAPER_KEY", "KIS_APP_KEY"),
        ("KIS_APP_PAPER_SECRET", "KIS_APP_SECRET"),
        ("KIS_ACCOUNT_PAPER_NO", "KIS_ACCOUNT_NO"),
    )
    values: list[str] = []
    used: list[str] = []
    for paper_name, real_name in names:
        name = paper_name if paper and _optional_env(paper_name) else real_name
        value = _optional_env(name)
        if not value:
            hint = f"{paper_name} 또는 {real_name}" if paper else real_name
            raise RuntimeError(
                f"Missing required environment variable: {hint}. "
                "Copy env.example to .env and fill in your KIS credentials."
            )
        values.append(value)
        used.append(name)
    return values[0], values[1], values[2], used[2]


def _cache_path(env_name: str, fallback: Path) -> Path:
    """명시한 경로가 있으면 그것, 없으면 모드별 기본값.

    빈 문자열은 미설정으로 본다 — `.env`에 `SONTRADER_TOKEN_CACHE=`만 남겨
    두는 흔한 실수가 캐시를 현재 디렉토리로 보내는 것을 막는다.
    """
    raw = os.environ.get(env_name)
    return Path(raw).expanduser() if raw else fallback


def load_settings() -> Settings:
    load_dotenv()
    paper = os.environ.get("KIS_PAPER", "true").strip().lower() not in ("false", "0", "no")
    app_key, app_secret, account_no, account_var = _load_credentials(paper)

    cano, _, acnt_prdt_cd = account_no.partition("-")
    if len(cano) != 8 or len(acnt_prdt_cd) != 2:
        raise RuntimeError(
            f"{account_var} must look like 12345678-01 (계좌번호 8자리-상품코드 2자리)."
        )
    # expanduser: .env의 "~/..."가 확장되지 않으면 리포지토리 안에 "~" 디렉토리가
    # 생겨 토큰이 저장소에 커밋될 뻔한 사고가 실제로 있었다.
    #
    # **명시한 경로가 우선한다.** 우회책으로 경로를 직접 넘기던 스크립트를
    # 깨뜨리지 않기 위해서다. 다만 그때는 모드 분리가 호출자 몫이 된다 —
    # 두 모드에 같은 경로를 주면 T31의 사고가 그대로 재현된다.
    token_cache = _cache_path("SONTRADER_TOKEN_CACHE", default_token_cache(paper))
    approval_key_cache = _cache_path(
        "SONTRADER_APPROVAL_KEY_CACHE", default_approval_key_cache(paper)
    )
    return Settings(
        app_key=app_key,
        app_secret=app_secret,
        cano=cano,
        acnt_prdt_cd=acnt_prdt_cd,
        paper=paper,
        token_cache=token_cache,
        approval_key_cache=approval_key_cache,
    )
