#!/usr/bin/env bash
# 디스크/메모리 경보 — nightly-agent systemd 타이머에서 함께 실행
# 임계치 초과 시 journald에 CRIT 로그 기록 (journalctl -p crit 로 확인)

WARN_PCT=85
CRIT_PCT=92

log() { logger -t disk-watchdog -p "$1" "$2"; echo "$2"; }

alert=0
while IFS= read -r line; do
    pct=$(echo "$line" | awk '{print $5}' | tr -d '%')
    mnt=$(echo "$line" | awk '{print $6}')
    [[ "$pct" =~ ^[0-9]+$ ]] || continue
    if (( pct >= CRIT_PCT )); then
        log user.crit "CRIT: $mnt 사용률 ${pct}% (임계치 ${CRIT_PCT}%)"
        alert=1
    elif (( pct >= WARN_PCT )); then
        log user.warning "WARN: $mnt 사용률 ${pct}% (임계치 ${WARN_PCT}%)"
        alert=1
    fi
done < <(df -h --output=pcent,target 2>/dev/null | tail -n +2)

# /tmp 자동 정리 (3일 이상 된 파일, 일반 사용자 소유)
find /tmp -maxdepth 1 -mtime +3 -user seoy \( -type f -o -type d \) -exec rm -rf {} + 2>/dev/null || true

# journal 크기 재확인 (300MB 초과 시 vacuum)
jsize=$(journalctl --disk-usage 2>/dev/null | grep -oP '[\d.]+(?= M)')
if [[ -n "$jsize" ]] && (( ${jsize%.*} > 300 )); then
    journalctl --vacuum-size=200M 2>/dev/null
fi

exit $alert
