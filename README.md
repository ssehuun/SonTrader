# SonTrader

Korean stock trading bot built on the [Korea Investment & Securities (KIS) open API](https://apiportal.koreainvestment.com).

Paper trading (모의투자) is the default; real trading requires an explicit `KIS_PAPER=false`.

## Setup

```sh
uv sync                 # install dependencies into .venv
cp env.example .env     # then fill in your KIS app key/secret and account number
```

App keys are issued per environment on the KIS Developers portal — a 모의투자 key only works
against the paper domain, so make sure the key matches your `KIS_PAPER` setting.

## Usage

```sh
uv run sontrader quote 005930           # 현재가 조회
uv run sontrader balance                # 계좌 잔고
uv run sontrader buy 005930 10          # 시장가 매수
uv run sontrader buy 005930 10 --price 70000   # 지정가 매수
uv run sontrader sell 005930 10 --price 72000  # 지정가 매도
```

## Development

```sh
uv run pytest           # run tests
uv run ruff check .     # lint
uv run ruff format .    # format
```
