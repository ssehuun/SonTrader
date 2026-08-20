#!/bin/sh
# 장 마감 후 일일 수집 체인: 일봉 → 워치리스트.
#
# cron 예시 (평일 16:00 KST). cron은 서버 타임존을 따르므로 둘 중 맞는 것을 쓴다:
#   KST 서버 : 0 16 * * 1-5 /path/to/SonTrader/scripts/daily_collect.sh
#   UTC 서버 : 0  7 * * 1-5 /path/to/SonTrader/scripts/daily_collect.sh
#
# **16:00보다 이르게 잡지 말 것.** collect-prices는 15:40(KST) 전에 실행하면
# 오늘 봉이 임시 종가라 저장하지 않고 건너뛴다 — 그날 워치리스트가 하루
# 묵은 데이터로 만들어진다 (src/sontrader/data/prices.py의 include_today 참고).
#
# collect-dart(공시 수집)는 뺐다 — 지금 실전 진입 트리거 기본값이
# SONTRADER_ENTRY_TRIGGER=watchlist(모멘텀 단독)라 공시 데이터가 필요
# 없다. EVENT 트리거로 전환할 때 선택적으로 다시 넣는다.
#
# &&로 묶는다: 앞 단계가 실패하면 뒤를 돌리지 않는다. 시세 수집이 실패했는데
# 옛 데이터로 워치리스트를 다시 만들면 그날의 워치리스트가 조용히 틀어진다.

set -e

cd "$(dirname "$0")/.." || exit 1

# uv 경로 찾기. cron은 인터랙티브 셸이 아니라 PATH가 최소한만 잡혀 있어
# `uv`를 그냥 부르면 실패한다 — 그렇다고 특정 머신 경로를 박아두면 다른
# 환경에서 안 돈다. 환경변수 → PATH → 흔한 설치 위치 순으로 찾는다.
#   UV=/some/path/uv scripts/daily_collect.sh   ← 위 어디에도 없을 때
if [ -z "$UV" ]; then
    UV=$(command -v uv 2>/dev/null) || true
fi
if [ -z "$UV" ]; then
    for candidate in \
        "$HOME/.local/bin/uv" \
        /opt/homebrew/bin/uv \
        /usr/local/bin/uv \
        /usr/bin/uv
    do
        if [ -x "$candidate" ]; then
            UV=$candidate
            break
        fi
    done
fi
if [ -z "$UV" ] || [ ! -x "$UV" ]; then
    echo "daily_collect: uv를 찾을 수 없습니다. UV=/path/to/uv 로 지정하세요." >&2
    exit 127
fi

ts() { date '+%Y-%m-%d %H:%M:%S %Z'; }

# 어느 단계에서 죽었든 반드시 흔적을 남긴다. cron은 조용히 실패하면 아무도
# 모르고, 그날 워치리스트가 없는 채로 다음 날 아침을 맞는다.
trap 'code=$?; [ "$code" -ne 0 ] && echo "[$(ts)] daily_collect 중단 (종료코드 $code)" >&2' EXIT

# 실패한 cron 실행을 나중에 추적하려면 무엇이 언제 돌았는지 남아야 한다.
echo "[$(ts)] daily_collect 시작 (uv=$UV)"

# --universe-scope structural: 과거 스냅샷 1,863일치와 같은 필터를 쓴다.
# 기본값 tradeable은 symbol_master의 **오늘** 플래그(관리종목·영업이익 등)를
# 보는데, 과거 마스터가 존재하지 않아 백테스트 이력은 structural로만 만들 수
# 있다. 여기만 tradeable로 두면 "백테스트가 샀을 종목을 실전은 안 사는" 괴리가
# 생겨, 성과 차이의 원인이 전략인지 필터인지 구분할 수 없게 된다.
#
# tradeable이 추가로 막던 것 중 과거에도 판정 가능한 것들은 이미 point-in-time
# 필터로 들어와 있다: 거래정지→volume==0, 저유동성→거래대금 하한,
# 기준가→그날 종가(is_penny).
#
# `&&`로 잇지 않는다. `set -e`는 `&&` 리스트 안에서 실패한 명령에 반응하지
# 않아서(POSIX), 아래 두 줄을 `a && b`로 쓰면 a가 실패해도 스크립트가
# 계속 진행해 "완료"를 찍고 exit 0으로 끝난다 — cron은 성공으로 본다.
# 실제로 그 상태였다. 순차 실행이면 set -e가 정상 동작한다.
"$UV" run sontrader collect-prices
"$UV" run sontrader build-universe --universe-scope structural

echo "[$(ts)] daily_collect 완료"
