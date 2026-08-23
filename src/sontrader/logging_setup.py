"""이벤트 로그 설정 (01문서 §6.6.2, §6.6.3).

## stdout 하나만 쓴다

파일 경로·회전을 애플리케이션이 알면 배포 환경에 묶인다. 핸들러는 stdout
하나뿐이고, 분배는 systemd/journald나 리다이렉트에 맡긴다.

## `cli.py`는 이 모듈을 쓰지 않는다

CLI의 `print`는 로그가 아니라 **명령어의 출력**이다. 사용자가 터미널에서
기다리는 결과물이라 타임스탬프·레벨 접두어가 붙으면 오히려 나빠진다.
이 모듈이 대상으로 삼는 것은 §6.6 표의 "이벤트 로그" — 상시 가동 프로세스가
"왜 죽었나 / 뭐가 느린가"에 답하기 위해 남기는 기록이다. 그래서 `configure()`를
부르는 곳은 `apps/live.py` 하나다.

라이브러리 계층(`adapters/`, `data/`, `engine/`)은 `getLogger(__name__)`으로
남기기만 하고 핸들러를 붙이지 않는다 — 붙일지 말지는 프로세스가 정한다.

## 마스킹은 포매터에서 (§6.6.3)

"호출부마다 가리면 언젠가 빠뜨린다." 실제로 `raise_for_status()`를 응답 본문보다
먼저 불러서 **계좌번호가 박힌 URL이 트레이스백으로 노출된 사고**가 있었다.

필터(`logging.Filter`)가 아니라 포매터로 거는 이유는 **트레이스백 때문**이다.
필터는 `record.msg`/`record.args`만 만질 수 있고, `exc_info`가 문자열로 펼쳐지는
것은 포매팅 시점이다. 정작 사고가 났던 경로가 트레이스백이라 거기를 못 덮으면
의미가 없다.

두 겹으로 가린다:

1. **환경변수 값 그대로 치환** — 앱키·계좌번호·DB URL처럼 값을 아는 비밀.
   URL에 박히든 트레이스백에 박히든 문자열이 같으므로 확실하다.
2. **패턴 치환** — 접근토큰·웹소켓 접속키처럼 런타임에 발급돼 값을 미리 알 수
   없는 것. `"access_token": "..."`, `Bearer ...`, DB URL의 비밀번호 자리.
"""

from __future__ import annotations

import logging
import os
import re
import sys

REDACTED = "***"

# 값을 그대로 치환할 환경변수. 계좌번호는 URL 쿼리(CANO=...)에도 박히므로
# 8자리 앞부분만 따로 등록한다.
_SECRET_ENV_VARS = (
    "KIS_APP_KEY",
    "KIS_APP_SECRET",
    "KIS_ACCOUNT_NO",
    "DATABASE_URL",
    "POSTGRES_PASSWORD",
    "TELEGRAM_BOT_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "DART_API_KEY",
)

# 짧은 값을 등록하면 무관한 텍스트까지 뭉개진다("1" 같은 값이 들어오면
# 로그 전체가 ***가 된다). 비밀치고 이보다 짧은 것은 없다.
_MIN_SECRET_LEN = 6

_KEYED_SECRET = re.compile(
    r"(?i)\b(access_?token|approval_?key|app_?key|app_?secret|secret_?key|api_?key|"
    r"bot_?token|authorization|password)"
    r'(["\']?\s*[:=]\s*["\']?)'
    r"([^\s\"',&}]{6,})"
)
_BEARER = re.compile(r"(?i)\b(bearer\s+)([^\s\"']{6,})")
# postgresql://user:password@host — 비밀번호 자리만 가린다
_URL_PASSWORD = re.compile(r"(://[^:/@\s]+:)([^@/\s]+)(@)")


class SecretMaskingFormatter(logging.Formatter):
    """포맷된 문자열(트레이스백 포함) 전체에서 비밀을 가린다."""

    def __init__(self, fmt: str, *, secrets: tuple[str, ...] = ()) -> None:
        super().__init__(fmt)
        # 긴 것부터 지워야 짧은 값이 긴 값의 일부를 먼저 갉아먹지 않는다.
        self._secrets = tuple(sorted(set(secrets), key=len, reverse=True))

    def format(self, record: logging.LogRecord) -> str:
        return mask(super().format(record), self._secrets)


def collect_env_secrets() -> tuple[str, ...]:
    """환경에서 값을 아는 비밀을 모은다. 없으면 조용히 건너뛴다."""
    secrets: list[str] = []
    for name in _SECRET_ENV_VARS:
        value = (os.environ.get(name) or "").strip()
        if len(value) >= _MIN_SECRET_LEN:
            secrets.append(value)
    account_no = (os.environ.get("KIS_ACCOUNT_NO") or "").strip()
    cano, _, _ = account_no.partition("-")
    if len(cano) >= _MIN_SECRET_LEN:
        secrets.append(cano)  # URL 쿼리에는 CANO=12345678 형태로 8자리만 실린다
    return tuple(secrets)


def mask(text: str, secrets: tuple[str, ...] = ()) -> str:
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    # Bearer를 먼저 처리한다. `Authorization: Bearer <토큰>`을 _KEYED_SECRET에
    # 먼저 걸면 "authorization"의 값으로 "Bearer"만 잡아 가리고 정작 토큰이 남는다.
    text = _BEARER.sub(rf"\1{REDACTED}", text)
    text = _KEYED_SECRET.sub(rf"\1\2{REDACTED}", text)
    return _URL_PASSWORD.sub(rf"\1{REDACTED}\3", text)


def configure(level: str | None = None) -> None:
    """루트 로거에 stdout 핸들러 하나를 건다. 여러 번 불러도 안전하다.

    레벨은 `SONTRADER_LOG_LEVEL`(기본 INFO). 형식은 고정한다 — 배포마다
    다르면 grep·파싱 규칙을 매번 새로 써야 한다.
    """
    resolved = (level or os.environ.get("SONTRADER_LOG_LEVEL") or "INFO").upper()

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        SecretMaskingFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            secrets=collect_env_secrets(),
        )
    )
    root.addHandler(handler)
    root.setLevel(getattr(logging, resolved, logging.INFO))

    # 라이브러리 소음 차단. httpx는 요청마다 INFO를 찍는데, 60초 사이클이면
    # 우리 로그가 그 안에 묻힌다.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
