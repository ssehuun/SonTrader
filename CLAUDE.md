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
