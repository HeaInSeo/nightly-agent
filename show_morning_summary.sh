#!/bin/bash
# show_morning_summary.sh - 아침 수동 조회용 스크립트

# 가장 최근 Run 탐색
RUN_DIR=$(ls -td .nightly_agent/runs/*/ 2>/dev/null | head -1)

echo "====================================="
echo " 🌙 Nightly Agent Summary Report"
echo "====================================="

if [ -z "$RUN_DIR" ]; then
    echo "실행된(Run) 기록이 전혀 없습니다."
    exit 0
fi

echo "[가장 최근 Run 폴더: $RUN_DIR]"

SUMMARY_FILE="${RUN_DIR}summary.md"
if [ -f "$SUMMARY_FILE" ]; then
    cat "$SUMMARY_FILE"
else
    echo "summary.md 가 아직 생성되지 않았습니다. (3_morning_summary.py 를 실행하세요)"
fi

echo "====================================="
