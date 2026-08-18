# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

SonTrader is a Korean stock trading bot built on the Korea Investment & Securities (KIS) open
REST API (https://apiportal.koreainvestment.com). Python, managed with uv, src layout.

## Commands

```sh
uv sync                                        # install deps (incl. dev group) into .venv
uv run pytest                                  # run all tests
uv run pytest tests/test_client.py::test_get_quote   # run a single test
uv run ruff check .                            # lint
uv run ruff format .                           # format
uv run sontrader quote 005930                  # CLI (needs .env with KIS credentials)
uv run sontrader migrate                       # create/extend trading-state DB schema (needs DATABASE_URL)
uv run sontrader collect-dart                  # ingest today's DART disclosures (needs DART_API_KEY too)
uv run sontrader collect-master                # KOSPI/KOSDAQ symbol master → symbol_master
uv run sontrader collect-prices                # daily candles, 수정주가 (incremental + self-healing)
uv run sontrader build-universe                # momentum watchlist + daily snapshot (hysteresis 30/42)
```

## Architecture

Three layers, each in one module under `src/sontrader/`:

- `config.py` — `Settings` frozen dataclass built from env vars / `.env` (`load_settings()`).
  Selects the API base URL: paper trading (모의투자, `openapivts...:29443`) vs real
  (`openapi...:9443`). **Paper is the default**; real trading requires `KIS_PAPER=false`.
- `auth.py` — `TokenManager` issues the 24h OAuth token and caches it on disk
  (`~/.cache/sontrader/token.json` by default). Caching matters: KIS invalidates the previous
  token on re-issue and rate-limits issuance. The cache records the issuing base URL because
  paper and real tokens are not interchangeable.
- `client.py` — `KisClient` wraps the REST endpoints (quote, balance, cash orders). Every KIS
  call needs a `tr_id` header that differs between real and paper environments; the mapping is
  the `_TR_IDS` table — add new endpoints there. API-level failures (`rt_cd != "0"`) raise
  `KisError`; HTTP failures raise `httpx.HTTPStatusError`.
- `cli.py` — argparse CLI (`sontrader` entry point) over the client.
- `data/db.py` — SQLAlchemy Core schema + `migrate()` for the trading-state tables (events,
  llm_judgments, orders, fills, positions, approvals) in the PostgreSQL DB shared with the
  legacy kis_trading collectors (`DATABASE_URL`). Also adds adjusted-price columns to the
  legacy `stock_candles_1d`. Schema tests run on SQLite in-memory — no DB server needed.
- `data/dart.py` — OpenDART 공시 수집기: `DartClient.list_disclosures()` (list.json, paginated,
  유가/코스닥 only) + `ingest()` (append-only into `events`, idempotent via ON CONFLICT,
  dual timestamps published_at/ingested_at, `norm_key` strips 정정 prefixes). CLI:
  `sontrader collect-dart [--date YYYYMMDD] [--interval N]`.
- `data/master.py` — KOSPI/KOSDAQ .mst 종목 마스터 다운로드·고정폭 파싱 (kis_trading 명세
  포팅, pandas 없이) → `symbol_master` upsert. Flags stay raw ('Y'/'N') — 해석은 core 필터.
- `data/prices.py` — 일봉 수집기: 수정주가(FID_ORG_ADJ_PRC="0"), 100일 창 페이징, 증분 수집,
  겹침 구간 종가 대조로 기업행위 감지 시 종목 전체 재수집 (자가치유), 일시 오류 재시도.
- `data/universe.py` — 워치리스트 스냅샷 빌더: 마스터 필터 → 유동성(20일 평균 거래대금) →
  모멘텀 → 히스테리시스 → `watchlist_snapshots` (같은 날 재실행 시 동일 결과).
- `core/` — 순수 함수만, 부작용 없음 (momentum.py, watchlist.py 히스테리시스 30/42,
  filters.py 방어 필터). DB/네트워크/시각 접근 금지 — 백테스트와 실전이 같은 코드를 쓰는 전제.

KIS API responses put data in `output` / `output1` / `output2` keys with all values as strings
(e.g. prices come back as `"71000"`).

## Testing conventions

No network in tests: `KisClient` and `TokenManager` accept an injected httpx transport, and
tests use `httpx.MockTransport` handlers that also answer `/oauth2/tokenP` (see
`make_client` in `tests/test_client.py`). Shared fixtures (`settings`, `TOKEN_RESPONSE`) live
in `tests/conftest.py`; `settings` points the token cache at `tmp_path`.

## Credentials

Live in `.env` (gitignored); `env.example` is the template. KIS app keys are issued per
environment — a 모의투자 key only works against the paper domain.

## 구현 계획

- @~/Downloads/01-요구사항-설계-확정.md, @~/Downloads/02-코드-구조.md, @~/Downloads/03-아키텍처-데이터흐름.svg, @~/Downloads/04-매매-생애주기.svg, @~/Downloads/05-기존레포-이관계획.md 파일들이 구현 계획서니까 이대로 구현을 해야해
- 모든 구현은은 내가 리뷰를 할 수 있게 한꺼번에 구현하지 말고 기능단위로 구현을 하고 구현후에 리뷰를 하면서 보안, 구조적, 논리적으로 오류가 있는지 검토해