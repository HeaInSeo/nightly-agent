# Nightly Agent (나이틀리 에이전트)

본 프로젝트는 Gemma 4와 샌드박스 환경 기반 자율 에이전트(Autonomous Sandbox Agent)를 활용해 매일 밤 복수의 코드 저장소를 자동으로 감사하고 버그 픽스를 제안하는 파이프라인(Code Review & Fix Pipeline)입니다.

## 아키텍처 및 핵심 개념 (Architecture & Concepts)

에이전트는 원본 코드를 즉시 변경하는 방식의 위험성을 배제하고, 철저히 통제된 **타임 트리거(Time-Triggered) + 상태 기반(State-Based)** 파이프라인 3단계를 통해 작동하도록 안전하게 설계되었습니다. 개발자가 프로젝트를 직접 건드리지 않고, 야간에만 안전하게 스크립트가 구동됩니다.

### 1단계: 코드 리뷰 (`1_nightly_review.py`)
- **작업 내용**: 지정된 설정(`projects.yaml`)에 따라 기준 브랜치(Base Branch)와 현재 HEAD 사이의 변경 사항(Diff)을 추출합니다.
- **안전 검증**: 안전을 위해 변경점이 너무 많은 경우(`config.json`의 `max_diff_lines` 제한 초과 등) 즉시 수정을 건너뜁니다.
- **결과물**: LLM을 통해 사람을 위한 종합 마크다운 파일 `review_report.md`와 기계를 위한 `issues.json` 명세서를 생성합니다.

### 2단계: 자동 수정 후보군 샌드박스 테스트 (`2_nightly_fix_candidate.py`)
- **작업 내용**: LLM이 분석한 이슈를 바탕으로 패치(Patch) 코드를 생성합니다. 
- **샌드박스 격리**: `git worktree` 명령어 기반의 `auto-fix-{run_id}` 임시 환결을 생성하여 기존 프로젝트의 파일에 아무런 손상을 주지 않고 패치 적용 여부(`git apply --check`)를 검증합니다.
- **테스트 및 검증**: 패치를 적용해보고, 프로젝트에 지정된 `test_command`(예: `go test`, `dotnet test`)를 실행하여 동작을 확인합니다.
- **안전 보장(Cleanup)**: 샌드박스에서 수행된 모든 작업 및 브랜치는 성공 여부와 관계없이 스크립트 종료 부분(`finally`)에서 완전히 파기되며, 최적의 `.patch` 파일 경로만 기록으로 남깁니다.

### 3단계: 아침 요약 보고서 (`3_morning_summary.py`)
- **작업 내용**: 그 전날 밤부터 새벽까지 진행된 각 프로젝트들의 상태값(State)과 이슈 발생 수치, 생성된 패치의 경로 등을 조회합니다.
- **결과물**: 루트 폴더 또는 지정된 경로에 모든 프로젝트의 현황을 요약한 `summary.md` 파일을 작성합니다. 이 요약본 안에는 LLM 모델 정보(`model_name`)도 기록됩니다. 개발자는 아침 출근 시 이 파일 하나만 확인하면 됩니다.

## 설정 및 실행 방법 (Setup)

1. `config.json` 수정 및 `configs/projects.yaml` 프로젝트 다중 등록
   - `model_name`, 글로벌 설정 언어(`language: "ko"|"en"`) 등을 원하는 대로 작성합니다.
   - 분석할 여러 프로젝트들의 경로, 프로파일(타입), 포함/제외 필터 파일 포맷을 각각 설정에 기입합니다.
2. 시스템에 Ollama 설치 및 기동
   - 루트 폴더의 `setup-ollama.sh` 등을 활용해 서비스가 동작하는지 확인합니다.
3. 파이프라인 오케스트레이션 실행 (스케줄링)
   - 크론탭(Crontab) 등에 `python3 nightly_run_all.py`를 매일 밤(예: 02:00) 지정해 두면, 설정된 모든 프로젝트를 순회하며 리뷰와 픽스를 진행합니다.
4. 모닝 요약본 확인
   - 사용자는 다음날 아침 `./show_morning_summary.sh`를 구동하거나, 생성된 `.nightly_agent/runs/...` 밑의 최신 디렉토리 속 파일들을 읽어보시면 됩니다.

---

*This document was updated to provide Korean documentation natively based on user request.*
