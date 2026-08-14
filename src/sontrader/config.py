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

DEFAULT_TOKEN_CACHE = Path.home() / ".cache" / "sontrader" / "token.json"
DEFAULT_APPROVAL_KEY_CACHE = Path.home() / ".cache" / "sontrader" / "approval_key.json"


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


def load_settings() -> Settings:
    load_dotenv()
    try:
        app_key = os.environ["KIS_APP_KEY"]
        app_secret = os.environ["KIS_APP_SECRET"]
        account_no = os.environ["KIS_ACCOUNT_NO"]
    except KeyError as exc:
        raise RuntimeError(
            f"Missing required environment variable {exc}. "
            "Copy env.example to .env and fill in your KIS credentials."
        ) from exc

    cano, _, acnt_prdt_cd = account_no.partition("-")
    if len(cano) != 8 or len(acnt_prdt_cd) != 2:
        raise RuntimeError(
            "KIS_ACCOUNT_NO must look like 12345678-01 (계좌번호 8자리-상품코드 2자리)."
        )

    paper = os.environ.get("KIS_PAPER", "true").strip().lower() not in ("false", "0", "no")
    # expanduser: .env의 "~/..."가 확장되지 않으면 리포지토리 안에 "~" 디렉토리가
    # 생겨 토큰이 저장소에 커밋될 뻔한 사고가 실제로 있었다.
    token_cache = Path(os.environ.get("SONTRADER_TOKEN_CACHE", DEFAULT_TOKEN_CACHE)).expanduser()
    approval_key_cache = Path(
        os.environ.get("SONTRADER_APPROVAL_KEY_CACHE", DEFAULT_APPROVAL_KEY_CACHE)
    ).expanduser()
    return Settings(
        app_key=app_key,
        app_secret=app_secret,
        cano=cano,
        acnt_prdt_cd=acnt_prdt_cd,
        paper=paper,
        token_cache=token_cache,
        approval_key_cache=approval_key_cache,
    )
