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

import functools
import inspect
import logging
import os
import re
import sys
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, TypeVar

from sontrader.timeutil import KST

REDACTED = "***"

F = TypeVar("F", bound=Callable[..., Any])

# @traced가 인자·반환값을 찍을 때의 길이 상한. 봉 300개나 종목 2,464개짜리
# 리스트가 그대로 펼쳐지면 한 줄이 수만 자가 되어 로그를 못 읽는다.
_TRACE_REPR_LIMIT = 80

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
    """포맷된 문자열(트레이스백 포함) 전체에서 비밀을 가리고, 시각은 KST로 찍는다."""

    def __init__(self, fmt: str, *, secrets: tuple[str, ...] = ()) -> None:
        super().__init__(fmt)
        # 긴 것부터 지워야 짧은 값이 긴 값의 일부를 먼저 갉아먹지 않는다.
        self._secrets = tuple(sorted(set(secrets), key=len, reverse=True))

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        """KST 고정. 기본 구현은 머신 타임존을 따르는데, 이 서버는 UTC라
        로그가 09:22로 찍히고 DB의 `cycle_log.ts`(naive KST)는 18:22이 된다.
        시각을 대조할 수 없으면 "그때 무슨 일이 있었나"를 추적할 수 없다.

        밀리초를 유지한다. 기본 구현이 붙여 주는 것을 이 재정의가 떨어뜨리면
        초 단위 해상도가 되어, 한 사이클 안의 순서(주문 제출 연속, 재시도,
        `traced`의 진입/이탈 짝)를 로그만으로 복원할 수 없다 — 소요시간을
        남기는 목적과 정면으로 어긋난다.
        """
        stamped = datetime.fromtimestamp(record.created, KST)
        if datefmt:
            return stamped.strftime(datefmt)
        return f"{stamped.strftime('%Y-%m-%d %H:%M:%S')},{int(record.msecs):03d}"

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
            "%(asctime)s KST %(levelname)s %(name)s %(message)s",
            secrets=collect_env_secrets(),
        )
    )
    root.addHandler(handler)
    root.setLevel(getattr(logging, resolved, logging.INFO))

    # 라이브러리 소음 차단. httpx는 요청마다 INFO를 찍는데, 60초 사이클이면
    # 우리 로그가 그 안에 묻힌다.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


def _brief(value: object) -> str:
    """로그 한 줄에 넣을 수 있는 짧은 표현."""
    try:
        text = repr(value)
    except Exception:  # noqa: BLE001 — 추적 로그가 본 기능을 깨뜨리면 안 된다
        return f"<{type(value).__name__}>"
    if len(text) > _TRACE_REPR_LIMIT:
        return f"{text[:_TRACE_REPR_LIMIT]}…({len(text)}자)"
    return text


def traced(fn: F) -> F:
    """진입·이탈·소요시간을 DEBUG로 남긴다. **경계 함수에만** 붙인다.

    경계란 프로세스 밖(KIS API, DB, 텔레그램)과 닿는 지점과 사이클의 큰
    단계다. `core/`에는 붙이지 않는다 — 순수 함수이고 종목마다 호출되므로
    사이클당 수백 줄이 되어 정작 필요한 줄이 묻힌다. 그리고 `core/`가
    부작용을 갖는 순간 "백테스트와 실전이 같은 코드를 실행한다"는 전제
    (설계 §1.1 원칙 1)가 깨진다.

    소요시간이 함께 남는 것이 핵심이다. KIS는 유량 한도가 있어(모의 초당 2건)
    "어느 호출이 느렸나"를 알아야 사이클 예산을 짤 수 있다. 지금 붙어 있는
    곳은 상시 가동 경로뿐이다 — `cli.py`는 `configure()`를 부르지 않으므로
    (그 모듈의 `print`는 로그가 아니라 명령어 출력이다) 수집기 커맨드에
    `SONTRADER_LOG_LEVEL=DEBUG`를 걸어도 효과가 없다.

    **레벨과 무관하게 예외는 ERROR로 남기고 그대로 다시 올린다.** 진입·이탈
    줄만 DEBUG로 가린다 — 예전에는 DEBUG가 아니면 `try`에 들어가기도 전에
    조기 반환해서, docstring이 약속한 실패 로그가 기본 레벨에서 영원히
    찍히지 않았다. 관측을 위해 붙인 데코레이터가 정작 실패를 감추고 있었다.
    """
    log = logging.getLogger(fn.__module__)

    # self/cls는 매번 같아서 정보가 없으니 인자 표시에서 뺀다. 판정은 데코레이션
    # 시점에 한 번만 한다 — 호출 시점에 `hasattr(type(args[0]), fn.__name__)`으로
    # 추측하면, 첫 인자의 타입이 우연히 같은 이름의 속성을 가진 경우 실제 인자를
    # 조용히 삼킨다.
    try:
        first_param = next(iter(inspect.signature(fn).parameters), None)
    except (TypeError, ValueError):  # 시그니처를 못 읽는 콜러블
        first_param = None
    skip_first = first_param in ("self", "cls")

    @functools.wraps(fn)
    def wrapper(*args: object, **kwargs: object) -> object:
        debug = log.isEnabledFor(logging.DEBUG)
        if debug:
            shown = args[1:] if skip_first else args
            joined = ", ".join(
                [_brief(a) for a in shown] + [f"{k}={_brief(v)}" for k, v in kwargs.items()]
            )
            log.debug("→ %s(%s)", fn.__qualname__, joined)
        started = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            log.error(
                "← %s 실패 %.0fms: %s: %s",
                fn.__qualname__,
                (time.perf_counter() - started) * 1000,
                type(exc).__name__,
                exc,
            )
            raise
        if debug:
            elapsed = (time.perf_counter() - started) * 1000
            log.debug("← %s %.0fms → %s", fn.__qualname__, elapsed, _brief(result))
        return result

    return wrapper  # type: ignore[return-value]
