"""텔레그램 봇 어댑터 (구현 계획 6단계). 01문서 §6.3.

## 인바운드 포트를 열지 않는다

01문서 §6.3: "인터넷에 노출된 관리자 페이지는 계좌 조작이 가능한 공격면이
된다." 웹훅 대신 `getUpdates` 롱폴링을 쓰는 이유가 이것이다 — 이 프로세스는
텔레그램 서버로 나가는 연결만 열고, 들어오는 연결은 받지 않는다.

## 이 어댑터가 하는 일

01문서 §6.3 표의 4가지: 진입 승인/거부(인라인 버튼), 체결·손절·장애
알림(푸시), 킬 스위치(명령어), 포지션·스톱 상태 조회(명령어). 백테스트
결과(로컬 HTML 리포트)는 텔레그램과 무관하므로 여기 없다.

## `process_update()`가 DB와 HTTP를 함께 다루는 이유

`adapters/broker_kis.py`와 같은 이유다 — 이 어댑터는 텔레그램이라는 특정
외부 시스템과 그에 관련된 도메인 상태(승인 큐, 킬 스위치)를 함께 다룬다.
결정 자체(`engine/approval.py`, `engine/killswitch.py`)는 순수 DB 로직이고,
여기서는 그 결과를 텔레그램 사용자에게 확인해 주는 부분만 얹는다.

## 아직 하지 않는 것

`/positions`, `/stops`(포지션·스톱 상태 조회)는 봉 데이터(`BarView`)가
있어야 트레일링 스톱을 계산할 수 있는데, 이 어댑터는 DB만 안다. 지금은
대기 중인 승인과 킬 스위치 상태만 보여주는 `/status`로 대신한다 — 실제
포지션·스톱 조회는 `apps/live.py`가 `Context`를 조립할 수 있게 된 다음
슬라이스로 미룬다(YAGNI, 02문서 §7). `propose()`를 실행 루프에 연결하는
일과 킬 스위치로 신규 진입을 실제로 거르는 일도 마찬가지로 다음 슬라이스다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

import httpx
from sqlalchemy.engine import Engine

from sontrader.engine import approval, killswitch


class Notifier(Protocol):
    """`engine/loop.py`가 필요로 하는 만큼만 — 알림 발송 두 가지.

    구현체는 `TelegramNotifier` 하나뿐이지만, `engine/loop.py`가 이 어댑터
    모듈에 직접 묶이지 않도록(엔진 계층이 구체 구현이 아니라 프로토콜에
    의존하도록) 여기 별도로 둔다.
    """

    def send_message(self, text: str) -> None: ...
    def send_approval_request(self, proposal: approval.Proposal) -> None: ...


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

    def send_approval_request(self, proposal: approval.Proposal) -> None:
        """진입 승인 요청 — 인라인 버튼 2개(승인/거부). `callback_data`에
        `동작:proposal_id`를 실어 보내고, `process_update()`가 콜백을 받으면
        그대로 파싱해 `engine.approval.decide()`를 호출한다."""
        text = (
            f"진입 승인 요청\n"
            f"종목: {proposal.symbol}\n"
            f"비중: {proposal.weight:.0%}\n"
            f"만료: {proposal.expires_at.isoformat()}"
        )
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "승인", "callback_data": f"approve:{proposal.proposal_id}"},
                    {"text": "거부", "callback_data": f"reject:{proposal.proposal_id}"},
                ]
            ]
        }
        self._call(
            "sendMessage", {"chat_id": self._chat_id, "text": text, "reply_markup": keyboard}
        )

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        """버튼을 누른 사용자 화면의 로딩 스피너를 해제한다 — 텔레그램이
        요구하는 절차이며, 안 부르면 클라이언트에서 계속 로딩 중으로 보인다."""
        self._call("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})

    def get_updates(self, *, offset: int | None = None, timeout: int = 0) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        return self._call("getUpdates", params)

    def process_update(self, update: dict[str, Any], *, now: datetime) -> None:
        """`get_updates()`가 돌려준 항목 하나를 처리한다."""
        if "callback_query" in update:
            self._process_callback(update["callback_query"], now=now)
        elif "message" in update:
            self._process_command(update["message"], now=now)

    def _process_callback(self, callback_query: dict[str, Any], *, now: datetime) -> None:
        data = callback_query.get("data", "")
        action, _, proposal_id = data.partition(":")
        query_id = callback_query["id"]
        if action not in ("approve", "reject") or not proposal_id:
            self._safe_answer_callback_query(query_id, "알 수 없는 요청입니다.")
            return

        try:
            proposal = approval.decide(
                self._engine, proposal_id, approve=(action == "approve"), now=now
            )
        except approval.ProposalNotFoundError:
            self._safe_answer_callback_query(query_id, "존재하지 않는 제안입니다.")
            return
        except approval.ProposalNotPendingError:
            self._safe_answer_callback_query(query_id, "이미 처리됐거나 만료된 제안입니다.")
            return

        verb = "승인" if action == "approve" else "거부"
        self._safe_answer_callback_query(query_id, f"{proposal.symbol} {verb}했습니다.")
        self.send_message(f"{proposal.symbol} 진입 {verb}됨 (제안 {proposal.proposal_id[:8]})")

    def _safe_answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        """`answer_callback_query()`가 실패해도 무시한다.

        텔레그램의 콜백은 유효시간이 있어 응답이 늦으면(폴링 주기가 길거나
        일시적으로 밀리면) HTTP 400으로 거절될 수 있다. 이건 버튼의 로딩
        스피너를 못 지우는 UX 문제일 뿐이다 — `approval.decide()`는 이미
        반영됐으므로, 이 실패 때문에 뒤따르는 `send_message()` 확인 알림까지
        막히면 안 된다(실전 테스트 중 실제로 이 순서로 재현됐다).
        """
        try:
            self.answer_callback_query(callback_query_id, text)
        except (TelegramError, httpx.HTTPError):
            pass

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
        pending = approval.list_pending(self._engine)
        lines = [
            f"킬 스위치: {'작동 중' if engaged else '해제'}",
            f"대기 중 승인: {len(pending)}건",
        ]
        for proposal in pending:
            lines.append(
                f"  - {proposal.symbol} ({proposal.weight:.0%}, "
                f"만료 {proposal.expires_at.isoformat()})"
            )
        return "\n".join(lines)

    def _call(self, method: str, payload: dict[str, Any]) -> Any:
        response = self._http.post(f"/{method}", json=payload)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise TelegramError(f"{method} failed: {data.get('description')}")
        return data["result"]
