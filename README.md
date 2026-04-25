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
| `llm.api_base_url` | OpenAI 호환 LLM 엔드포인트 (Ollama: `http://localhost:11434/v1`) |
| `llm.model_name` | 사용할 모델명 |
| `llm.api_key` | API 키 (Ollama는 빈 문자열) |
| `github.token` | GitHub Personal Access Token |
| `github.reports_repo` | 리포트를 push할 GitHub 저장소 (`owner/repo`) |
| `scan_paths` | `na scan`이 탐색할 로컬 디렉토리 목록 |
| `clone_roots` | `na add` 시 타입별 클론 기본 경로 |
| `max_diff_lines` | 이 줄 수 초과 diff는 리뷰 스킵 |
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
  {project}/issues.md               ← 누적 이슈 현황 (자동 업데이트)
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

`llm.api_base_url`만 변경하면 모든 OpenAI 호환 엔진을 사용할 수 있습니다.

| 엔진 | api_base_url |
|------|-------------|
| Ollama | `http://localhost:11434/v1` |
| vLLM | `http://localhost:8000/v1` |
| LM Studio | `http://localhost:1234/v1` |
| OpenAI | `https://api.openai.com/v1` |
