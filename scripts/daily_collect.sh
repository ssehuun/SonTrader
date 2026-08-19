#!/bin/sh
# 장 마감 후 일일 수집 체인: 일봉 → 워치리스트.
# cron으로 매일 16:00 KST(=07:00 UTC, 이 서버 타임존) 평일 실행 — crontab 참고.
#
# collect-dart(공시 수집)는 뺐다 — 지금 실전 진입 트리거 기본값이
# SONTRADER_ENTRY_TRIGGER=watchlist(모멘텀 단독)라 공시 데이터가 필요
# 없다. EVENT 트리거로 전환할 때 선택적으로 다시 넣는다.
#
# &&로 묶는다: 앞 단계가 실패하면 뒤를 돌리지 않는다. 시세 수집이 실패했는데
# 옛 데이터로 워치리스트를 다시 만들면 그날의 워치리스트가 조용히 틀어진다.
#
# cron은 인터랙티브 셸이 아니라 PATH가 최소한만 잡혀 있어 uv를 전체 경로로 부른다.

set -e

cd "$(dirname "$0")/.." || exit 1

UV=/home/shn413.jung/.local/bin/uv

"$UV" run sontrader collect-prices \
  && "$UV" run sontrader build-universe
