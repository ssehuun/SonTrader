# SonTrader

[KIS 오픈 API](https://apiportal.koreainvestment.com) 기반 국내 주식 스윙 트레이딩 봇.
모멘텀 워치리스트(기본) 또는 DART 공시로 후보를 뽑아, **진입은 텔레그램 승인 후 다음 개장
시가, 청산은 완전 자동 즉시** 집행한다. 기본값은 모의투자 — 실전은 `KIS_PAPER=false` 명시.

## 설치

```sh
uv sync                 # 의존성 설치 (dev 포함)
cp env.example .env     # 자격증명 입력 — 항목 설명은 env.example 주석
```

앱키는 환경별 발급이라 `KIS_PAPER`와 짝을 맞춰야 한다. 자격증명 3개
(`KIS_APP_KEY`·`KIS_APP_SECRET`·`KIS_ACCOUNT_NO`)면 조회·수동 주문은 동작한다.

| 추가로 필요한 곳 | 환경변수 |
|---|---|
| DB 명령 전체 (`migrate`, `collect-*`, `backtest`, 실전) | `DATABASE_URL` 또는 `POSTGRES_*` |
| `collect-dart` | `DART_API_KEY` ([opendart.fss.or.kr](https://opendart.fss.or.kr), 무료) |
| `backtest --llm`, 실전 `event` 트리거 | `ANTHROPIC_API_KEY` (백테스트는 `OPENAI_API_KEY`도 가능) |
| 실전 승인 큐·알림·킬 스위치 | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |

### DB는 클라우드에, 백필 전에

`backfill-prices`는 수 시간짜리 일회성 작업이라, DB가 로컬 PC에만 있으면 머신을 옮길 때마다
다시 해야 한다. Neon·Supabase 무료 티어로 충분하고(실전 붙일 땐 유료 — 가용성이 곧 손실),
전환은 `DATABASE_URL` 교체 후 `migrate` 한 번이다.

## 명령어

### 조회 / 수동 주문

```sh
uv run sontrader quote 005930                   # 현재가
uv run sontrader balance                        # 잔고
uv run sontrader buy 005930 10                  # 시장가 매수
uv run sontrader buy 005930 10 --price 70000    # 지정가 매수
uv run sontrader sell 005930 10 --price 72000   # 지정가 매도
```

### 수집 (로컬 PC에서 실행)

```sh
uv run sontrader migrate                        # 매매 상태 DB 스키마
uv run sontrader collect-master                 # KOSPI/KOSDAQ 종목 마스터
uv run sontrader collect-dart                   # 오늘자 DART 공시 → events (멱등)
uv run sontrader collect-dart --date 20260101 --interval 60   # 특정일 / 주기 폴링
uv run sontrader collect-prices                 # 일봉 (수정주가, 증분+자가치유)
uv run sontrader collect-prices --limit 50      # 앞쪽 N종목만 (테스트용)
uv run sontrader backfill-prices --dry-run      # 과거 백필 규모 추정
uv run sontrader backfill-prices --earliest 20180101
uv run sontrader build-universe                 # 모멘텀 워치리스트 + 일별 스냅샷
```

`scripts/daily_collect.sh`가 일봉 → 워치리스트를 묶어 돌린다 (cron용).

### 백테스트

```sh
uv run sontrader backtest --start 20250101 --end 20251231
uv run sontrader backtest --start 20250101 --end 20251231 --llm   # LLM 진입 판단 포함
uv run sontrader backtest --start 20250101 --end 20251231 --llm \
  --llm-provider openai --llm-model gpt-5 --llm-base-url https://api.openai.com/v1
```

`--llm` 없으면 청산 로직만 검증한다(신규 진입 없음). `--initial-cash` 기본 1,000만원.
결과: CAGR·샤프·MDD·승률·손익비·평균 보유일·거래비용 비중 (표본 30건 미만이면 경고).

### 실전 실행

```sh
uv run python -m sontrader.apps.live
```

기동 시 KIS 잔고와 DB 포지션을 대조한 뒤(불일치면 매매 중단, 알림만), 텔레그램 폴링과
60초 매매 사이클을 반복한다. `Ctrl-C`/SIGTERM으로 정상 종료.

| 항목 | 동작 |
|---|---|
| 진입 | 텔레그램 인라인 버튼 승인 후 다음 사이클 |
| 청산 | 완전 자동, LLM 불필요 |
| 진입 트리거 | `SONTRADER_ENTRY_TRIGGER` — `watchlist`(기본) \| `event`(공시+LLM) |
| 텔레그램 명령 | `/kill` 매매 중단 · `/resume` 재개 · `/status` 상태 |
| 텔레그램 없을 때 | 동작은 하지만 승인·알림·킬 스위치 불가 |
| 1분봉 | 웹소켓으로 수집해 저장만 — 판정은 일봉 기준 |
| 휴장일 | 실전 계좌만 캘린더 확인. 모의투자는 해당 API 미지원 |

## 주의

- **수집은 15:40 이후에.** 그 전에 `collect-prices`를 돌리면 오늘 봉이 임시 종가라 저장되지
  않는다. 저장해버리면 청산이 임시 종가로 발동하고 스냅샷이 재현 불가능해진다.
- **`collect-prices`는 앞으로만 간다.** `--lookback-days`를 키워도 과거는 안 늘어난다.
  기간을 늘리려면 `backfill-prices`(중단·재실행 안전, 기존 행 불변).
- **백테스트 가능 구간 = 보유 거래일 − 253.** 모멘텀이 253 거래일을 워밍업으로 소비한다
  (2년치 ≈ 743 거래일 ≈ 1,110 달력일). 과거로 갈수록 생존 편향도 커진다 — 상장폐지 종목은
  `symbol_master`에 없어 그 시점 유니버스에서 이미 빠져 있다.
- **상시 가동 전제지만 systemd·백업·워치독이 없다.** 지금은 사람이 켜두고 지켜봐야 한다.

## 개발

```sh
uv run pytest                                        # 전체 테스트
uv run pytest tests/test_client.py::test_get_quote   # 단일 테스트
uv run ruff check .                                  # 린트
uv run ruff format .                                 # 포맷
```

## 더 알아보기

| 문서 | 내용 |
|---|---|
| `CLAUDE.md` | 아키텍처, 모듈별 책임, 테스트 컨벤션 |
| `docs/02-코드-구조.md` | 단계별 구현 현황과 설계 결정 이유 |
| `docs/01-요구사항-설계-확정.md` | 요구사항 원문, 미확정 파라미터(§8) |
| `docs/scenario/` | 매크로 국면 판단 설계 + 가설 원장(append-only) |
| `todo/` | 미룬 작업. `01-실전-차단.md`가 실전을 막는 것들 |
