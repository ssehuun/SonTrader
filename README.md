# SonTrader

Korean stock trading bot built on the [Korea Investment & Securities (KIS) open API](https://apiportal.koreainvestment.com).
DART 공시를 신호로, 모멘텀 팩터로 선별한 워치리스트 안에서 국내 주식을 매매하는 스윙 트레이딩
시스템이다 — 진입은 텔레그램으로 사람 승인 후 다음 개장 시가, 청산은 완전 자동 즉시 집행.

Paper trading (모의투자) is the default; real trading requires an explicit `KIS_PAPER=false`.

## Setup

```sh
uv sync                 # install dependencies (incl. dev group) into .venv
cp env.example .env     # then fill in credentials — see the comments in env.example
```

App keys are issued per environment on the KIS Developers portal — a 모의투자 key only works
against the paper domain, so make sure the key matches your `KIS_PAPER` setting.

`env.example`이 필요한 환경변수 전체 목록이다. 최소한 KIS 자격증명(`KIS_APP_KEY`,
`KIS_APP_SECRET`, `KIS_ACCOUNT_NO`)만 있으면 시세 조회·주문 명령이 동작한다. 아래 표는
기능별로 추가로 필요한 것들이다.

| 기능 | 추가로 필요한 환경변수 |
|---|---|
| DB 관련 명령 (`migrate`, `collect-*`, `build-universe`, `backtest`, 실전 실행) | `DATABASE_URL` 또는 `POSTGRES_*` |
| `collect-dart` | `DART_API_KEY` ([opendart.fss.or.kr](https://opendart.fss.or.kr), 무료) |
| `backtest --llm`, 실전 실행 (진입 판단) | `ANTHROPIC_API_KEY` (또는 백테스트는 `OPENAI_API_KEY`도 가능) |
| 실전 실행의 승인 큐·알림·킬 스위치 | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |

## CLI 사용법

### 조회 / 수동 주문

```sh
uv run sontrader quote 005930                   # 현재가 조회
uv run sontrader balance                        # 계좌 잔고
uv run sontrader buy 005930 10                  # 시장가 매수
uv run sontrader buy 005930 10 --price 70000    # 지정가 매수
uv run sontrader sell 005930 10 --price 72000   # 지정가 매도
```

### 데이터 수집 (로컬 PC에서 실행 — 01문서 §6.1 역할 분리)

```sh
uv run sontrader migrate                        # 매매 상태 DB 스키마 생성/확장
uv run sontrader collect-master                 # KOSPI/KOSDAQ 종목 마스터
uv run sontrader collect-dart                   # 오늘자 DART 공시 → events (멱등)
uv run sontrader collect-dart --date 20260101 --interval 60   # 특정일, 주기 폴링
uv run sontrader collect-prices                 # 일봉 수집 (수정주가, 증분+자가치유)
uv run sontrader collect-prices --limit 50      # 상위 N종목만 (테스트용)
uv run sontrader build-universe                 # 모멘텀 워치리스트 산출 + 일별 스냅샷 저장
```

`collect-dart`/`collect-prices`/`build-universe`는 매일 장 시작 전 실행해야 그날의 이벤트·
시세·워치리스트가 쌓인다 — cron이나 systemd timer로 스케줄링한다.

### 백테스트

```sh
uv run sontrader backtest --start 20250101 --end 20251231
# LLM 진입 판단 포함 (--llm 없으면 청산 로직만 검증, 신규 진입 없음)
uv run sontrader backtest --start 20250101 --end 20251231 --llm
uv run sontrader backtest --start 20250101 --end 20251231 --llm \
  --llm-provider openai --llm-model gpt-5 --llm-base-url https://api.openai.com/v1
```

`--initial-cash`(기본 1,000만원)로 초기 자본을 바꿀 수 있다. 결과에 CAGR·샤프·MDD·승률·
손익비·평균 보유일·거래비용 비중이 출력된다(표본 30건 미만이면 경고).

### 실전 실행 (장중 상시 가동)

```sh
uv run python -m sontrader.apps.live
```

아직 `sontrader` 서브커맨드로 연결되지 않아 모듈로 직접 실행한다. 기동 시 KIS 잔고와 DB
포지션을 대조하고(불일치 시 매매를 시작하지 않고 알림만 보낸다), 이후 텔레그램 폴링(승인/
거부 버튼, `/kill`·`/resume`·`/status` 명령)과 매매 사이클(약 60초 주기)을 반복한다.
`Ctrl-C`(SIGINT) 또는 SIGTERM으로 정상 종료된다.

- 진입은 텔레그램 인라인 버튼으로 승인해야 다음 사이클에 주문이 나간다. 청산은 완전 자동이다.
- `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`가 없으면 텔레그램 없이도 동작하지만, 승인 요청·
  알림·킬 스위치를 쓸 수 없다.
- `ANTHROPIC_API_KEY`가 없으면 신규 진입 판단을 하지 않고 청산(스톱)만 자동으로 동작한다.
- 워치리스트가 있으면 웹소켓으로 1분봉을 실시간 수집해 DB에 쌓지만(`stock_candles_1m`),
  진입/청산 판정 자체는 아직 일봉 기준이다 — 분봉 기준 파라미터가 검증되기 전까지의 의도적
  선택이다(자세한 이유는 `docs/02-코드-구조.md` "분봉 수집기" 절 참고).
- 실전 계좌(`KIS_PAPER=false`)에서는 국내휴장일조회로 거래일 캘린더를 확인한다 — 휴장일이면
  그날은 매매 사이클을 건너뛰고 텔레그램으로 하루 한 번만 알린다(텔레그램 명령 응답은 계속
  동작). 모의투자는 이 API를 지원하지 않아 캘린더 확인 자체를 건너뛴다.
- **상시 가동을 전제로 만들어졌지만(01문서 §6.2), 자동 재시작(systemd)·백업·워치독은 아직
  없다** — 지금은 사람이 직접 켜두고 지켜봐야 한다.

## Development

```sh
uv run pytest                                        # run all tests
uv run pytest tests/test_client.py::test_get_quote    # run a single test
uv run ruff check .                                   # lint
uv run ruff format .                                  # format
```

## 더 알아보기

- `CLAUDE.md` — 아키텍처, 모듈별 책임, 테스트 컨벤션
- `docs/02-코드-구조.md` — 단계별 구현 현황과 설계 결정 이유
- `docs/01-요구사항-설계-확정.md` — 요구사항·설계 원문, 미확정 파라미터 목록(§8)
