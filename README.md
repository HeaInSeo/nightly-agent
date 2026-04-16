# Nightly Agent (나이틀리 에이전트)

본 프로젝트는 Gemma 4와 샌드박스 환경 기반 자율 에이전트(Autonomous Sandbox Agent)를 활용해 매일 밤 복수의 코드 저장소를 자동으로 감사하고 버그 픽스를 제안하는 파이프라인(Code Review & Fix Pipeline)입니다.

개발자는 저녁에 작업을 마치고 나면, 다음날 아침 Discord 메시지로 리뷰 결과를 수신합니다.

## 아키텍처 및 핵심 개념 (Architecture & Concepts)

에이전트는 원본 코드를 즉시 변경하는 방식의 위험성을 배제하고, 철저히 통제된 **타임 트리거(Time-Triggered) + 상태 기반(State-Based)** 파이프라인 3단계를 통해 작동하도록 안전하게 설계되었습니다. 개발자가 프로젝트를 직접 건드리지 않고, 야간에만 안전하게 스크립트가 구동됩니다.

### 1단계: 코드 리뷰 (`1_nightly_review.py`)
- **작업 내용**: 지정된 설정(`projects.yaml`)에 따라 기준 브랜치(Base Branch)와 현재 HEAD 사이의 변경 사항(Diff)을 추출합니다.
- **안전 검증**: 안전을 위해 변경점이 너무 많은 경우(`config.json`의 `max_diff_lines` 제한 초과 등) 즉시 수정을 건너뜁니다.
- **결과물**: LLM을 통해 사람을 위한 종합 마크다운 파일 `review_report.md`와 기계를 위한 `issues.json` 명세서를 생성합니다.

### 2단계: 자동 수정 후보군 샌드박스 테스트 (`2_nightly_fix_candidate.py`)
- **작업 내용**: LLM이 분석한 이슈를 바탕으로 패치(Patch) 코드를 생성합니다.
- **샌드박스 격리**: `git worktree` 명령어 기반의 `auto-fix-{run_id}` 임시 환경을 생성하여 기존 프로젝트의 파일에 아무런 손상을 주지 않고 패치 적용 여부(`git apply --check`)를 검증합니다.
- **테스트 및 검증**: 패치를 적용해보고, 프로젝트에 지정된 `test_command`(예: `go test`, `dotnet test`)를 실행하여 동작을 확인합니다.
- **안전 보장(Cleanup)**: 샌드박스에서 수행된 모든 작업 및 브랜치는 성공 여부와 관계없이 스크립트 종료 부분(`finally`)에서 완전히 파기되며, 최적의 `.patch` 파일 경로만 기록으로 남깁니다.

### 3단계: 아침 요약 보고서 (`3_morning_summary.py`)
- **작업 내용**: 그 전날 밤부터 새벽까지 진행된 각 프로젝트들의 상태값(State)과 이슈 발생 수치, 패치 검증 결과를 조회합니다.
- **결과물**: `summary.md` 파일 생성 및 Discord 웹훅으로 요약 전송. 개발자는 아침에 Discord 메시지 하나만 확인하면 됩니다.

## 설정 및 실행 방법 (Setup)

### 1. 의존성 설치
```bash
pip install -r requirements.txt
```

### 2. `config.json` 설정
```json
{
  "ollama_url": "http://localhost:11434/api/generate",
  "model_name": "gemma4:26b",
  "max_diff_lines": 2000,
  "max_retries": 3,
  "base_branch": "master",
  "language": "ko",
  "discord_webhook_url": "https://discord.com/api/webhooks/..."
}
```
- `discord_webhook_url`: Discord 채널의 웹훅 URL을 입력합니다. 비워두면 Discord 전송을 건너뜁니다.

### 3. `configs/projects.yaml`에 프로젝트 등록
분석할 프로젝트들의 경로, 타입, 포함/제외 파일 패턴을 설정합니다.

### 4. Ollama 설치 및 모델 실행
```bash
./setup-ollama.sh
```

### 5. crontab 등록 (야간 자동 실행)
```bash
# 매일 새벽 2시 자동 실행
0 2 * * * cd /path/to/nightly-agent && venv/bin/python3 nightly_run_all.py
```

## 실행 방법

```bash
# 전체 프로젝트 실행
venv/bin/python3 nightly_run_all.py

# 특정 프로젝트만 실행
venv/bin/python3 nightly_run_all.py --project tori

# run-id 직접 지정
venv/bin/python3 nightly_run_all.py --run-id 2026-04-16T02-00-00

# 아침 요약만 재생성
venv/bin/python3 3_morning_summary.py --run-id 2026-04-16T02-00-00
```

## 결과물 위치

```
.nightly_agent/runs/{run_id}/{project_name}/
  ├── state.json          ← 파이프라인 상태 추적
  ├── review_report.md    ← LLM 코드 리뷰 보고서
  ├── issues.json         ← 발견된 이슈 목록
  ├── candidate_1.patch   ← LLM이 생성한 패치 파일 (참고용)
  └── candidate_1_test.log
```

## 지원 프로젝트 타입

| 타입 | 언어/환경 | 기본 테스트 명령 |
|------|----------|----------------|
| `go` | Go | `go test ./...` |
| `dotnet` | C# / .NET | `dotnet test` |
| `infra` | Terraform / Shell | `terraform validate` |
| `shell` | Shell Script | `shellcheck` |
