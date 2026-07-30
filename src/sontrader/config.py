"""Environment-based configuration.

Credentials come from environment variables; a local ``.env`` file is
loaded automatically. See ``env.example`` for the full list.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

REAL_BASE_URL = "https://openapi.koreainvestment.com:9443"
PAPER_BASE_URL = "https://openapivts.koreainvestment.com:29443"

DEFAULT_TOKEN_CACHE = Path.home() / ".cache" / "sontrader" / "token.json"


@dataclass(frozen=True)
class Settings:
    app_key: str
    app_secret: str
    cano: str  # 종합계좌번호: first 8 digits of the account number
    acnt_prdt_cd: str  # 계좌상품코드: 2 digits after the dash
    paper: bool = True
    token_cache: Path = field(default=DEFAULT_TOKEN_CACHE)

    @property
    def base_url(self) -> str:
        return PAPER_BASE_URL if self.paper else REAL_BASE_URL


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
    token_cache = Path(os.environ.get("SONTRADER_TOKEN_CACHE", DEFAULT_TOKEN_CACHE))
    return Settings(
        app_key=app_key,
        app_secret=app_secret,
        cano=cano,
        acnt_prdt_cd=acnt_prdt_cd,
        paper=paper,
        token_cache=token_cache,
    )
