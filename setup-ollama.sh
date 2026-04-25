#!/bin/bash
# setup-ollama.sh - Install Ollama, setup model, and register crontab
# 실행 시간과 마감 시간은 config.json의 cron_hour/cron_min/deadline_hour/deadline_minute로 설정합니다.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/venv/bin/python3"
CONFIG_FILE="$SCRIPT_DIR/config.json"

# config.json에서 스케줄 설정 읽기 (없으면 기본값 사용)
_PARSE_HOUR='
import json, re, sys
def parse_hour(v):
    if v is None: return None
    if isinstance(v, int): return v
    m = re.match(r"^(\d{1,2})(am|pm)$", str(v).lower().strip())
    if m:
        h, ap = int(m.group(1)), m.group(2)
        if ap == "pm" and h != 12: h += 12
        elif ap == "am" and h == 12: h = 0
        return h
    return int(v)
'

if [ -f "$CONFIG_FILE" ]; then
    CRON_HOUR=$(python3 -c "$_PARSE_HOUR
c=json.load(open('$CONFIG_FILE'))
v=parse_hour(c.get('cron_hour',2))
print(v if v is not None else 2)" 2>/dev/null || echo 2)

    CRON_MIN=$(python3 -c "import json; print(json.load(open('$CONFIG_FILE')).get('cron_min', 0))" 2>/dev/null || echo 0)

    MODEL_NAME=$(python3 -c "import json; print(json.load(open('$CONFIG_FILE')).get('model_name', 'qwen2.5:72b'))" 2>/dev/null || echo "qwen2.5:72b")

    DEADLINE_HOUR=$(python3 -c "$_PARSE_HOUR
c=json.load(open('$CONFIG_FILE'))
v=parse_hour(c.get('deadline_hour'))
print(v if v is not None else '')" 2>/dev/null || echo "")

    DEADLINE_MIN=$(python3 -c "import json; print(json.load(open('$CONFIG_FILE')).get('deadline_minute', 0))" 2>/dev/null || echo 0)
else
    CRON_HOUR=2
    CRON_MIN=0
    MODEL_NAME="qwen2.5:72b"
    DEADLINE_HOUR=""
    DEADLINE_MIN=0
fi

echo "=== Nightly Agent Setup ==="
echo ""
echo "스케줄 설정:"
echo "  시작 시간: 매일 ${CRON_HOUR}시 ${CRON_MIN}분"
if [ -n "$DEADLINE_HOUR" ]; then
    echo "  마감 시간: ${DEADLINE_HOUR}시 ${DEADLINE_MIN}분 (이후 남은 프로젝트는 스킵)"
else
    echo "  마감 시간: 없음"
fi

# 1. Install Ollama
echo ""
echo "1. Installing Ollama..."
curl -fsSL https://ollama.com/install.sh | sh

# 2. Ollama 모델 경로 설정 (/data500)
echo ""
echo "2. Ollama 모델 경로 설정..."
sudo mkdir -p /data500/ollama/models
sudo chown -R ollama:ollama /data500/ollama
sudo mkdir -p /etc/systemd/system/ollama.service.d
echo -e "[Service]\nEnvironment=\"OLLAMA_MODELS=/data500/ollama/models\"" \
    | sudo tee /etc/systemd/system/ollama.service.d/models-path.conf > /dev/null

# 3. Start Ollama service
echo ""
echo "3. Checking Ollama service status..."
sudo systemctl daemon-reload
if ! systemctl is-active --quiet ollama; then
    sudo systemctl enable ollama
    sudo systemctl start ollama
else
    sudo systemctl restart ollama
fi

# 4. Pull model
echo ""
echo "4. Verifying required model: $MODEL_NAME"
if ollama list | grep -q "$MODEL_NAME"; then
    echo "Model $MODEL_NAME is already installed."
else
    echo "Model $MODEL_NAME not found. Pulling now..."
    echo "(대용량 모델입니다. 네트워크 속도에 따라 시간이 걸릴 수 있습니다.)"
    ollama pull "$MODEL_NAME"
    if [ $? -ne 0 ]; then
        echo "모델 다운로드 실패. 네트워크 또는 RAM을 확인해주세요."
        exit 1
    fi
    echo "모델 다운로드 완료."
fi

# 5. Setup virtualenv and install dependencies
echo ""
echo "5. Setting up Python virtualenv..."
if [ ! -f "$PYTHON" ]; then
    python3 -m venv "$SCRIPT_DIR/venv"
fi
"$PYTHON" -m pip install -q -r "$SCRIPT_DIR/requirements.txt"
echo "의존성 설치 완료."

# 6. Install na CLI
echo ""
echo "6. Installing na CLI..."
sudo ln -sf "$SCRIPT_DIR/na.py" /usr/local/bin/na
sudo chmod +x "$SCRIPT_DIR/na.py"
echo "na CLI 설치 완료. (na start / na stop / na scan / na config)"

# 7. Register systemd timer (crontab 대체)
echo ""
echo "7. Registering systemd timer..."

# timer의 OnCalendar를 config의 cron_hour/cron_min으로 동적 생성
TIMER_HOUR=$(printf "%02d" "$CRON_HOUR")
TIMER_MIN=$(printf "%02d" "$CRON_MIN")

cat > /tmp/nightly-agent.service << EOF
[Unit]
Description=Nightly Agent — 야간 코드 리뷰 파이프라인
After=network-online.target ollama.service
Wants=ollama.service

[Service]
Type=oneshot
User=$(whoami)
WorkingDirectory=$SCRIPT_DIR
ExecStart=$PYTHON $SCRIPT_DIR/nightly_run_all.py
StandardOutput=journal
StandardError=journal
SyslogIdentifier=nightly-agent

[Install]
WantedBy=multi-user.target
EOF

cat > /tmp/nightly-agent.timer << EOF
[Unit]
Description=Nightly Agent 타이머 — 매일 ${TIMER_HOUR}시 ${TIMER_MIN}분 실행
Requires=nightly-agent.service

[Timer]
OnCalendar=*-*-* ${TIMER_HOUR}:${TIMER_MIN}:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo cp /tmp/nightly-agent.service /etc/systemd/system/nightly-agent.service
sudo cp /tmp/nightly-agent.timer /etc/systemd/system/nightly-agent.timer
sudo systemctl daemon-reload
sudo systemctl enable nightly-agent.timer
sudo systemctl start nightly-agent.timer

if systemctl is-active --quiet nightly-agent.timer; then
    echo "systemd 타이머 등록 완료."
    echo "  스케줄: 매일 ${TIMER_HOUR}시 ${TIMER_MIN}분"
    echo "  로그: journalctl -u nightly-agent"
    echo "  제어: na start / na stop"
else
    echo "타이머 등록 실패. sudo 권한을 확인해주세요."
fi

mkdir -p "$SCRIPT_DIR/.nightly_agent"

echo ""
echo "=== 설정 완료 ==="
echo "수동 실행: cd $SCRIPT_DIR && $PYTHON nightly_run_all.py"
echo "제어 명령: na start / na stop / na scan / na config"
echo "로그 확인: journalctl -u nightly-agent -f"
echo ""
echo "시간 변경: config.json의 cron_hour/cron_min 수정 후 ./setup-ollama.sh 재실행"
