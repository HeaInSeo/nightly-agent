# 기술 부채

---

## [TD-001] 첫 실행 시 diff 크기 초과로 리뷰가 skip될 수 있음

**상태**: 미해결  
**발견**: 2026-05-05  
**우선순위**: P1

### 현상

`last_reviewed_commit..HEAD` 방식(B방식)에서, 프로젝트를 처음 등록하거나 `.nightly_agent/last_reviewed/{project}.json`이 없는 경우 `base_branch` 값으로 fallback한다. 현재 기본값은 `HEAD~3`.

그러나 `HEAD~3`보다 오래된 커밋이 많거나, 신규 클론 직후처럼 커밋 히스토리가 길 경우 실제로는 `HEAD~3` 이후 diff가 `max_diff_lines`(현재 2000줄)를 초과해 `skipped`로 끝날 수 있다.

### 재현 조건

1. 프로젝트를 새로 클론하고 `na scan` 또는 `na add`로 등록
2. `.nightly_agent/last_reviewed/{project}.json` 파일 없음
3. `base_branch: HEAD~3` fallback으로 첫 diff 실행
4. diff가 2000줄 초과 → `status_p1_review: skipped`

### 현재 동작

`get_filtered_diff()`에서 `last_reviewed_commit`이 없으면:
```
git diff HEAD~3..HEAD -- ...
```
이 결과가 `max_diff_lines`를 넘으면 조용히 skip된다.

### 해결 방안 (미구현)

**안 A**: `na add` 또는 `na scan` 시 자동으로 현재 HEAD를 `last_reviewed_commit`으로 시드
```python
save_last_reviewed(project_name, current_head)
```
장점: 이후 실행부터 오늘 이후 커밋만 리뷰. 단점: 첫날 기존 코드 리뷰 없음.

**안 B**: 첫 실행 감지 시 `max_diff_lines`를 더 작은 값으로 제한하고 가장 최근 N줄만 리뷰
장점: 첫날도 일부 리뷰. 단점: 구현 복잡.

**안 C**: 등록 시 "초기 리뷰 스킵" 여부를 config에서 선택
```yaml
first_run_mode: skip  # or "recent_N_commits"
```

### 임시 workaround (TD-001)

신규 프로젝트 등록 후 직접 시드:
```bash
python3 -c "
from agent_core import save_last_reviewed
from agent_core import run_git
head, _, _ = run_git(['rev-parse', 'HEAD'], cwd='/path/to/project')
save_last_reviewed('project_name', head.strip())
"
```

---

## [TD-002] remote_first 모드에서 build/test가 로컬 코드 기준으로 실행됨

**상태**: 미해결  
**발견**: 2026-05-10  
**우선순위**: P2

### 현상

`remote_first: true` 설정 시 diff는 `origin/{branch}` HEAD 기준으로 생성되지만,
`collect_command_results()`(build/test/lint)는 여전히 **로컬 워킹트리** 기준으로 실행된다.
로컬 클론이 원격보다 뒤처진 경우 빌드·테스트 결과가 실제 원격 코드를 반영하지 못한다.

### 재현 조건

1. `remote_first: true` 프로젝트에서 다른 머신이 새 커밋을 push
2. nightly-agent 서버의 로컬 클론은 pull 안 된 상태 (stale)
3. nightly-agent 실행 → diff는 원격 커밋 기준이나, `go test ./...` 등은 구버전 로컬 코드 기준

### 해결 방안 (미구현): Worktree 방식

원격 HEAD 커밋 기준으로 임시 worktree를 생성해 diff·build·test를 모두 그 안에서 실행한 후 삭제.

```python
# 개념 코드
run_git(["worktree", "add", f".nightly_agent/sandbox_worktree/{run_id}", remote_head], cwd=cwd)
# → 이 경로에서 collect_command_results() + get_filtered_diff() 실행
run_git(["worktree", "remove", "--force", f".nightly_agent/sandbox_worktree/{run_id}"], cwd=cwd)
```

**장점**: 로컬 워킹트리 완전 비손상, 원격 코드 정확 반영, 오픈소스 범용 사용 가능  
**단점**: 디스크 사용(프로젝트 크기만큼), cleanup 필수, 구현 복잡도 증가

**참고**: `.gitignore`에 이미 `.nightly_agent/sandbox_worktree/` 등록되어 있어
Phase 2(`2_nightly_fix_candidate.py`)의 worktree 패턴과 동일하게 확장 가능.
