# Nightly Agent

매일 밤 복수의 코드 저장소를 자동으로 리뷰하고 버그 픽스를 제안하는 파이프라인입니다.
결과는 GitHub 저장소(`nightly-agent-reports`)에 마크다운 문서로 누적됩니다.

## 파이프라인 구조

```
Phase -1  원격 저장소 sync (git pull --ff-only)
Phase 0.5 이슈 연속성 체크 (기존 이슈 해결/재발 추적)
Phase 1   코드 리뷰 → issues.json + review_report.md 생성
Phase 2   자동 픽스 후보 생성 → .patch 파일 (git worktree 샌드박스)
Phase 3   아침 요약 → summary.md → GitHub 리포트 저장소 push
```

## 설치

```bash
sudo ./setup-ollama.sh
```

Ollama 설치, 모델 다운로드, Python venv 구성, `na` CLI 등록, systemd 타이머 등록이 한 번에 완료됩니다.

## CLI 사용법 (`na`)

```bash
na start                          # systemd 타이머 활성화 (매일 새벽 자동 실행)
na stop                           # systemd 타이머 비활성화
na scan                           # scan_paths 탐색 → 대화형 프로젝트 등록
na add https://github.com/org/repo  # GitHub 저장소 클론 + 자동 등록
na config                         # GitHub 토큰/저장소 및 clone_roots 설정
na review-level                   # 현재 리뷰 레벨 설정 조회
na review-level 2 --max-level 3   # 기본 리뷰 레벨 변경
na model                          # 현재 모델/설치된 Ollama 모델 조회
na model qwen3.6:27b              # 설치된 모델 중 하나로 변경
na model tune qwen3.6-nightly     # num_ctx/num_predict를 줄인 배치용 alias 생성
```

## 수동 실행

```bash
# 전체 프로젝트
venv/bin/python3 nightly_run_all.py

# 특정 프로젝트만
venv/bin/python3 nightly_run_all.py --project dag-go
```

## 로그 확인

```bash
journalctl -u nightly-agent -f
```

## 설정 파일

### `config.json`

`config.json.example`을 복사해서 수정합니다.

```bash
cp config.json.example config.json
na config   # GitHub 토큰/저장소 및 clone_roots 대화형 설정
```

| 필드 | 설명 |
|------|------|
| `llm.api_base_url` | 현재는 Ollama 엔드포인트만 지원 (`http://localhost:11434/v1`) |
| `llm.model_name` | 사용할 모델명 |
| `llm.api_key` | API 키 (Ollama는 빈 문자열) |
| `llm.timeout_seconds` | LLM 요청 타임아웃 초 |
| `llm.reasoning_effort` | Ollama OpenAI 호환 reasoning 제어 (`none`, `low`, `medium`, `high`) |
| `llm.disable_thinking` | Qwen thinking 모델에 `/no_think` 시스템 지시 추가 |
| `llm.review_max_tokens` | Phase 1 리뷰 응답 최대 토큰 수 |
| `llm.continuity_max_tokens` | Phase 0.5 연속성 체크 응답 최대 토큰 수 |
| `llm.fix_max_tokens` | Phase 2 패치 응답 최대 토큰 수 |
| `github.enabled` | `true`일 때만 GitHub 리포트 저장소에 push |
| `github.token` | GitHub Personal Access Token |
| `github.reports_repo` | 리포트를 push할 GitHub 저장소 (`owner/repo`) |
| `scan_paths` | `na scan`이 탐색할 로컬 디렉토리 목록 |
| `clone_roots` | `na add` 시 타입별 클론 기본 경로 |
| `max_diff_lines` | 이 줄 수 초과 diff는 리뷰 스킵 |
| `review.level` | 기본 리뷰 깊이 레벨 (`1` 보수적, `3` 넓게 탐지) |
| `review.max_level` | 자동 승급 시 최대 레벨 |
| `review.auto_promote` | 최근 clean run이 누적되면 레벨 자동 승급 |
| `phase2.min_severity` | 자동 패치를 시도할 최소 severity (`high` 권장) |
| `language` | 리포트 언어 (`ko` / `en`) |
| `cron_hour` | 실행 시각 (예: `"2am"`, `2`) |
| `deadline_hour` | 이 시각 이후 남은 프로젝트 스킵 (예: `"6am"`, `null`이면 마감 없음) |

#### 마감 시간 설정 예시

새벽 2시에 시작해서 오전 6시까지만 실행하려면:

```json
"cron_hour": "2am",
"deadline_hour": "6am"
```

설정 변경 후 `sudo ./setup-ollama.sh` 재실행하면 타이머에 반영됩니다.

### Qwen/Ollama 배치 튜닝 권장값

Qwen thinking 모델은 Ollama에서 기본 thinking이 켜지고, 기본 생성 길이도 길게 잡히기 쉽습니다. 야간 배치 리뷰용으로는 아래 값을 권장합니다.

```json
"llm": {
  "model_name": "qwen3.6:27b",
  "timeout_seconds": 900,
  "reasoning_effort": "none",
  "disable_thinking": true,
  "review_max_tokens": 1400,
  "continuity_max_tokens": 600,
  "fix_max_tokens": 2400
},
"review": {
  "level": 1,
  "max_level": 3,
  "auto_promote": true
},
"phase2": {
  "min_severity": "high",
  "min_severity_by_level": {
    "1": "high",
    "2": "high",
    "3": "medium"
  }
}
```

Ollama 기본 모델 대신 배치용 alias를 만드는 것도 권장합니다.

```bash
na model tune qwen3.6-nightly
na model qwen3.6-nightly
```

이 alias는 현재 기본값으로 `num_ctx 8192`, `num_predict 2048`를 사용합니다.

### Phase 2 자동 패치 기준

Phase 2는 이제 더 보수적으로 동작합니다. 기본적으로 아래 조건을 만족하는 이슈만 자동 패치 대상으로 삼습니다.

- severity가 `high` 이상
- 대상 파일이 하나로 좁혀짐
- `anchor.file`과 `anchor.function`이 모두 있음
- 이슈 설명이 추측성 표현(`가능성`, `may`, `potential` 등) 위주가 아님
- `suggested_action`이 실제 수정 행동을 가리키는 구체 문장임

조건을 만족하지 않으면 `status_p2_fix: skipped`로 종료되고 `fix_note`에 이유가 남습니다.

### 리뷰 레벨

리뷰는 이제 레벨 기반으로 동작합니다.

- `level 1`: 가장 보수적. 명백한 버그 위주, false positive 억제 강함
- `level 2`: 균형형. 명백한 버그 + 근거 있는 중간 수준 위험 일부 포함
- `level 3`: 탐색형. medium 위험까지 넓게 탐지

`review.auto_promote: true`이면 최근 clean run이 누적될수록 자동으로 레벨이 올라갑니다.

- 최근 clean run 2회 이상: `+1`
- 최근 clean run 4회 이상: `+2`

최종 레벨은 `review.max_level`을 넘지 않습니다. 각 run의 실제 적용 레벨은 `state.json`과 `review_report.md`에 기록됩니다.

### `configs/projects.yaml`

`na scan` 또는 `na add`로 자동 등록됩니다. 수동 작성도 가능합니다.

```yaml
projects:
  - name: my-service
    path: /opt/go/src/github.com/owner/my-service
    github_url: https://github.com/owner/my-service
    base_branch: main
    review:
      include: ["*.go", "*.mod"]
      exclude: ["vendor/**"]
    commands:
      lint: "make lint"   # 프로파일 기본값 오버라이드
```

> `type` 필드는 없습니다. 프로젝트 타입은 런타임에 파일 목록으로 자동 감지됩니다.
> Go + TypeScript 혼합 프로젝트처럼 복수 타입도 자동으로 처리됩니다.

## 결과물 위치

```
.nightly_agent/
  issues_db/{project}.json          ← 누적 이슈 DB (UUID + anchor)
  runs/{run_id}/{project}/
    state.json                      ← 파이프라인 상태
    review_report.md                ← LLM 코드 리뷰
    issues.json                     ← 이번 실행 신규 이슈
    candidate_1.patch               ← 자동 생성 패치 (참고용)
```

GitHub 리포트 저장소:
```
{reports_repo}/
  reports/{project}.md              ← 프로젝트별 누적 이슈 현황
  README.md                         ← 프로젝트별 최신 요약
```

## 지원 타입 및 기본 명령

| 타입 | 언어/환경 | 빌드 | 테스트 | 린트 |
|------|----------|------|--------|------|
| `go` | Go | `go build ./...` | `go test ./...` | `golangci-lint run` |
| `dotnet` | C# / .NET | `dotnet build` | `dotnet test` | — |
| `python` | Python | `python -m compileall -q .` | `pytest` | `ruff check .` |
| `typescript` | TypeScript / JS | `tsc --noEmit` | `npm test` | `eslint .` |
| `rust` | Rust | `cargo build` | `cargo test` | `cargo clippy` |
| `infra` | Terraform / YAML | `terraform validate` | `terraform plan` | `terraform fmt -check` |
| `shell` | Shell Script | — | `bash -n` | `shellcheck` |

`configs/projects.yaml`의 `commands`로 오버라이드 가능합니다.

### 새 타입 추가

1. `configs/profiles/{type}.yaml` — `default_commands` + `heuristics`
2. `prompts/review/{type}.md.j2` — 리뷰 프롬프트 (없으면 `generic.md.j2` 사용)

## 지원 LLM 엔진

현재 구현은 Ollama 전용입니다. 실행 전 상태 확인이 Ollama API(`/api/tags`)를 사용하고, 설치 스크립트도 Ollama 모델 풀링을 전제로 합니다.

| 엔진 | api_base_url |
|------|-------------|
| Ollama | `http://localhost:11434/v1` |
