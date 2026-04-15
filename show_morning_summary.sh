#!/bin/bash
# show_morning_summary.sh - 아침 수동 조회용 CLI

RUN_ID=$(date +%Y-%m-%d)
RUN_DIR=".nightly_agent/runs/$RUN_ID"
STATE_FILE="$RUN_DIR/state.json"
REVIEW_FILE="$RUN_DIR/review_report.md"

echo "====================================="
echo " 🌙 Nightly Agent Summary Report"
echo "====================================="

if [ ! -d "$RUN_DIR" ]; then
    echo "어젯밤 실행된(Run) 기록이 없습니다 ($RUN_ID)."
    exit 0
fi

echo "[상태 요약]"
cat $STATE_FILE | grep "status"

echo ""
echo "[최상위 이슈 목록]"
if [ -f "$REVIEW_FILE" ]; then
    cat $REVIEW_FILE
else
    echo "리뷰 리포트가 아직 생성되지 않았습니다."
fi

echo ""
echo "[패치 후보 목록]"
ls -l $RUN_DIR/*.patch 2>/dev/null || echo "생성된 패치 후보가 없습니다."
echo "====================================="
