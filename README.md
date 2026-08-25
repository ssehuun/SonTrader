# SonTrader

[KIS 오픈 API](https://apiportal.koreainvestment.com) 기반 국내 주식 스윙 트레이딩 봇.
모멘텀 워치리스트(기본) 또는 DART 공시로 후보를 뽑아, **진입은 다음 개장 시가, 청산은 즉시**
집행한다. 둘 다 완전 자동 — 사람은 매매 판단에 개입하지 않는다. 기본값은 모의투자,
실전은 `KIS_PAPER=false` 명시.

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
| 실전 `event` 트리거 (기본 아님) | `ANTHROPIC_API_KEY` (백테스트는 API 키가 필요 없다) |
| 실전 알림·킬 스위치 | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |

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

분봉 (백테스트용, **실전 자격증명 필수**):

```sh
uv run sontrader collect-minutes --symbols 005930 --days 2       # 특정 종목
uv run sontrader collect-minutes --from-watchlist --limit 5      # 워치리스트 앞쪽 N종목
uv run sontrader collect-minutes --from-watchlist --days 365     # 1년치 (보관 상한)
```

`scripts/daily_collect.sh`가 일봉 → 워치리스트를 묶어 돌린다 (cron용).

### 백테스트

```sh
uv run sontrader backtest --start 20250101 --end 20251231          # 워치리스트 순위 진입 (기본)
uv run sontrader backtest --start 20250101 --end 20251231 --cooldown-days 5
uv run sontrader backtest --start 20250101 --end 20251231 \
  --entry-trigger event --llm                                       # 공시 + 저장된 LLM 판단
```

| 인자 | 기본 | 내용 |
|---|---|---|
| `--entry-trigger` | `watchlist` | 진입 촉발. `watchlist`=모멘텀 순위, `event`=공시 |
| `--llm` | 꺼짐 | `event` 전용. `event`인데 생략하면 신규 진입 0건 (청산 로직만 검증) |
| `--llm-model` | `claude-opus-5` | 어느 모델의 판단을 읽을지 (캐시 키의 일부) |
| `--cooldown-days` | 없음 | 청산 후 재진입 금지 일수. `watchlist` 모드의 유일한 재진입 제동 |
| `--initial-cash` | 1,000만원 | |

**백테스트는 LLM API를 호출하지 않는다.** `--llm`은 `llm_judgments`에 이미 저장된
판단만 읽는다 — 호출하면 재실행 결과가 달라지고, 그 시점에 없던 모델이 과거 구간에
섞인다. 판단을 남기는 곳은 실전 루프뿐이라, 실전을 돌린 적 없는 구간은 대부분
미스로 나온다(빠진 건수를 출력한다).

결과: CAGR·샤프·MDD·승률·손익비·평균 보유일·거래비용 비중 (표본 30건 미만이면 경고).

### 실전 실행

```sh
uv run python -m sontrader.apps.live
```

기동 시 KIS 잔고와 DB 포지션을 대조한 뒤(불일치면 매매 중단, 알림만), 텔레그램 폴링과
60초 매매 사이클을 반복한다. `Ctrl-C`/SIGTERM으로 정상 종료.

| 항목 | 동작 |
|---|---|
| 진입 | 게이트 통과 즉시 주문 (승인 절차 없음) |
| 청산 | 완전 자동, LLM 불필요 |
| 진입 트리거 | `SONTRADER_ENTRY_TRIGGER` — `watchlist`(기본, LLM 호출 없음) \| `event`(공시마다 LLM API 호출) |
| 텔레그램 명령 | `/kill` 매매 중단 · `/resume` 재개 · `/status` 상태 |
| 텔레그램 없을 때 | 매매는 그대로 돈다. 알림·킬 스위치만 없다 |
| 1분봉 | 웹소켓으로 수집해 `source='ws'`로 저장만 — 판정은 일봉 기준. 백테스트는 `source='rest'`(거래소 확정 봉)만 읽는다 |
| 휴장일 | 실전 계좌만 캘린더 확인. 모의투자는 해당 API 미지원 |
| 로그 | stdout으로만. 레벨은 `SONTRADER_LOG_LEVEL`(기본 INFO). 앱키·계좌번호·토큰은 자동 마스킹 |

## 주의

- **수집은 15:40 이후에.** 그 전에 `collect-prices`를 돌리면 오늘 봉이 임시 종가라 저장되지
  않는다. 저장해버리면 청산이 임시 종가로 발동하고 스냅샷이 재현 불가능해진다.
- **`collect-prices`는 앞으로만 간다.** `--lookback-days`를 키워도 과거는 안 늘어난다.
  기간을 늘리려면 `backfill-prices`(중단·재실행 안전, 기존 행 불변).
- **분봉은 실전 전용, 1년 상한.** `주식일별분봉조회`가 모의투자 미지원이라
  `KIS_PAPER=false`가 아니면 즉시 실패한다. `--days`는 **타임스탬프 기준**이라 09시에
  `--days 1`을 주면 하한이 전날 09시가 되어 전날 세션이 빠진다 — 거래일 N개를 원하면
  N+1을 준다. 종목당 약 980호출(하루 4호출 × 245거래일)이라 37종목이면 10시간 규모다.
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
