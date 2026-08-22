"""텔레그램 봇 어댑터 (구현 계획 6단계). 01문서 §6.3.

## 인바운드 포트를 열지 않는다

01문서 §6.3: "인터넷에 노출된 관리자 페이지는 계좌 조작이 가능한 공격면이
된다." 웹훅 대신 `getUpdates` 롱폴링을 쓰는 이유가 이것이다 — 이 프로세스는
텔레그램 서버로 나가는 연결만 열고, 들어오는 연결은 받지 않는다.

## 매매 지시 수단이 아니다

01문서 §6.3 표의 3가지만 한다: 체결·손절·장애 알림(푸시), 킬 스위치(명령어),
포지션·스톱 상태 조회(명령어). 백테스트 결과(로컬 HTML 리포트)는 텔레그램과
무관하므로 여기 없다.

진입 승인 버튼은 없다 — 사람이 건별로 매매를 승인·거부하면 실전이 백테스트가
검증한 전략과 달라진다(01문서 §1.1 원칙 1). 이 봇이 매매에 영향을 줄 수 있는
경로는 킬 스위치, 즉 신규 진입 전체를 세우는 것 하나뿐이다. 봇이 죽어도
매매는 그대로 돈다.

## `process_update()`가 DB와 HTTP를 함께 다루는 이유

`adapters/broker_kis.py`와 같은 이유다 — 이 어댑터는 텔레그램이라는 특정
외부 시스템과 그에 관련된 도메인 상태(킬 스위치)를 함께 다룬다. 상태 변경
자체(`engine/killswitch.py`)는 순수 DB 로직이고, 여기서는 그 결과를 텔레그램
사용자에게 확인해 주는 부분만 얹는다.

## 아직 하지 않는 것

`/positions`, `/stops`(포지션·스톱 상태 조회)는 봉 데이터(`BarView`)가
있어야 트레일링 스톱을 계산할 수 있는데, 이 어댑터는 DB만 안다. 지금은
킬 스위치 상태만 보여주는 `/status`로 대신한다 — 실제 포지션·스톱 조회는
`apps/live.py`가 `Context`를 조립할 수 있게 된 다음 슬라이스로 미룬다
(YAGNI, 02문서 §7).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
from sqlalchemy.engine import Engine

from sontrader.engine import killswitch


class TelegramError(RuntimeError):
    """텔레그램 API가 ``ok: false``로 답했다."""


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        engine: Engine,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._chat_id = chat_id
        self._engine = engine
        self._http = httpx.Client(
            base_url=f"https://api.telegram.org/bot{bot_token}",
            timeout=15.0,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> TelegramNotifier:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def send_message(self, text: str) -> None:
        """체결·손절·장애 알림 등 단방향 푸시."""
        self._call("sendMessage", {"chat_id": self._chat_id, "text": text})

    def get_updates(self, *, offset: int | None = None, timeout: int = 0) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        return self._call("getUpdates", params)

    def process_update(self, update: dict[str, Any], *, now: datetime) -> None:
        """`get_updates()`가 돌려준 항목 하나를 처리한다.

        받는 것은 명령어 메시지뿐이다. 인라인 버튼을 보내지 않으므로
        `callback_query`를 비롯한 나머지 갱신은 무시한다.
        """
        if "message" in update:
            self._process_command(update["message"], now=now)

    def _process_command(self, message: dict[str, Any], *, now: datetime) -> None:
        text = (message.get("text") or "").strip()
        if text == "/kill":
            killswitch.engage(self._engine, now=now)
            self.send_message(
                "킬 스위치 작동 — 신규 진입을 중단합니다. 청산은 계속 자동 집행됩니다."
            )
        elif text == "/resume":
            killswitch.disengage(self._engine, now=now)
            self.send_message("킬 스위치 해제 — 신규 진입을 재개합니다.")
        elif text == "/status":
            self.send_message(self._status_text())

    def _status_text(self) -> str:
        engaged = killswitch.is_engaged(self._engine)
        return f"킬 스위치: {'작동 중' if engaged else '해제'}"

    def _call(self, method: str, payload: dict[str, Any]) -> Any:
        response = self._http.post(f"/{method}", json=payload)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise TelegramError(f"{method} failed: {data.get('description')}")
        return data["result"]
