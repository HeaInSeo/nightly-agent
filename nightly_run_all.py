import os
import sys
import time
import datetime
import argparse
import subprocess
import importlib.util
import yaml
from agent_core import AgentState, check_ollama, parse_hour

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", help="특정 프로젝트만 실행 (미지정 시 전체 실행)", required=False)
    parser.add_argument("--run-id", help="run_id 직접 지정", required=False)
    return parser.parse_args()

def run_phase(script, extra_args):
    """단계 스크립트를 list argv 방식으로 실행하고 종료 코드를 반환한다.
    출력은 캡처하지 않고 터미널/cron 로그로 흘려보낸다."""
    result = subprocess.run([sys.executable, script] + extra_args)
    return result.returncode


def load_continuity_module():
    spec = importlib.util.spec_from_file_location("issue_continuity", "0_issue_continuity.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def fmt_elapsed(seconds):
    m, s = divmod(int(seconds), 60)
    return f"{m}분 {s}초" if m else f"{s}초"

def past_deadline(deadline_hour, deadline_minute):
    """현재 시각이 마감 시간을 지났는지 확인한다."""
    if deadline_hour is None:
        return False
    now = datetime.datetime.now()
    return (now.hour * 60 + now.minute) >= (deadline_hour * 60 + deadline_minute)


def sync_project(proj):
    """Phase -1: 원격 저장소와 동기화. 반환값: 'ok' | 'skipped' | 'conflict'."""
    path = proj.get("path", "")
    name = proj.get("name", "")
    branch = proj.get("base_branch", "main")

    # HEAD~N 형식이면 실제 브랜치가 아니라 스킵
    if branch.startswith("HEAD"):
        return "ok"

    if not os.path.isdir(os.path.join(path, ".git")):
        return "ok"

    def git(*args):
        return subprocess.run(
            ["git", "-C", path] + list(args),
            capture_output=True, text=True
        )

    ret = git("fetch", "origin", "--quiet")
    if ret.returncode != 0:
        print(f"  [{name}] fetch 실패 — 스킵 (오프라인?): {ret.stderr.strip()}")
        return "skipped"

    behind = git("rev-list", f"HEAD..origin/{branch}", "--count")
    if behind.returncode != 0 or behind.stdout.strip() == "0":
        return "ok"

    count = behind.stdout.strip()
    print(f"  [{name}] {count}개 커밋 뒤처짐. pull 시도...")
    pull = git("pull", "--ff-only", "origin", branch)
    if pull.returncode == 0:
        print(f"  [{name}] pull 완료.")
        return "ok"
    else:
        print(f"  [{name}] fast-forward 불가 — 스킵 (로컬 변경 충돌?): {pull.stderr.strip()}")
        return "conflict"

def main():
    args = parse_args()

    if not os.path.exists("configs/projects.yaml"):
        print("configs/projects.yaml missing. Exiting.")
        return

    with open("configs/projects.yaml", "r") as f:
        registry = yaml.safe_load(f)

    projects = registry.get("projects", [])
    if not projects:
        print("No projects registered. Exiting.")
        return

    if args.project:
        projects = [p for p in projects if p.get("name") == args.project]
        if not projects:
            print(f"Project '{args.project}' not found in configs/projects.yaml.")
            return

    agent = AgentState(run_id=args.run_id)
    run_id = agent.run_id
    config = agent.config
    llm_conf = config.get("llm", {})
    ollama_url = llm_conf.get("api_base_url", "http://localhost:11434/v1")
    model_name = llm_conf.get("model_name", "?")

    deadline_hour = parse_hour(config.get("deadline_hour"))
    deadline_minute = config.get("deadline_minute", 0)
    deadline_str = (
        f"{deadline_hour:02d}:{deadline_minute:02d}" if deadline_hour is not None else "없음"
    )

    # ① LLM 서비스 가용성 체크
    print(f"Checking LLM service at {ollama_url} ...")
    if not check_ollama(config):
        msg = (
            f"나이틀리 에이전트 실행 실패\n"
            f"Run: {run_id}\n"
            f"LLM 서비스가 응답하지 않습니다. ({ollama_url})\n"
            f"서비스 상태를 확인해주세요: systemctl status ollama"
        )
        print(msg)
        sys.exit(1)
    print("LLM service OK.")

    project_names = [p.get("name") for p in projects]
    single_mode = f" (단일 모드: {args.project})" if args.project else ""

    start_msg = (
        f"나이틀리 리뷰 시작{single_mode}\n"
        f"Run: {run_id}\n"
        f"대상: {', '.join(project_names)} ({len(projects)}개)\n"
        f"모델: {model_name}\n"
        f"마감: {deadline_str}"
    )
    print(start_msg)

    start_time = time.time()
    failed_projects = []
    deadline_skipped = []
    continuity_mod = load_continuity_module()

    for idx, proj in enumerate(projects):
        pname = proj.get("name")

        if past_deadline(deadline_hour, deadline_minute):
            deadline_skipped = [p.get("name") for p in projects[idx:]]
            print(f"마감 시간 초과 ({deadline_str}) — 스킵: {', '.join(deadline_skipped)}")
            break

        print(f"\n--- [{idx+1}/{len(projects)}] Processing Project: {pname} ---")

        print(f"[{pname}] Running Phase -1 (Sync)...")
        sync_project(proj)

        print(f"[{pname}] Running Phase 0.5 (Continuity Check)...")
        run_phase("0_issue_continuity.py", ["--project", pname, "--run-id", run_id])

        print(f"[{pname}] Running Phase 1 (Review)...")
        rc1 = run_phase("1_nightly_review.py", ["--project", pname, "--run-id", run_id])
        if rc1 != 0:
            print(f"[{pname}] Phase 1 exited with code {rc1}.")
            failed_projects.append((pname, "phase1", rc1))
        else:
            # 신규 이슈를 issues_db에 병합
            issues_file = os.path.join(
                ".nightly_agent", "runs", run_id, pname, "issues.json"
            )
            if os.path.exists(issues_file):
                import json as _json
                state_file = os.path.join(".nightly_agent", "runs", run_id, pname, "state.json")
                with open(issues_file) as _f:
                    run_issues = _json.load(_f)
                added = continuity_mod.merge_new_issues(pname, run_issues)
                false_positive_cleaned = continuity_mod.reconcile_missing_issues(pname, run_issues)
                if os.path.exists(state_file):
                    with open(state_file, "r") as sf:
                        st = _json.load(sf)
                    st["false_positive_cleanup_count"] = false_positive_cleaned
                    with open(state_file, "w") as sf:
                        _json.dump(st, sf, indent=4)
                print(
                    f"[{pname}] issues_db 업데이트: {added}개 신규 이슈 추가, "
                    f"{false_positive_cleaned}개 false positive 정리"
                )

        print(f"[{pname}] Running Phase 2 (Fix)...")
        rc2 = run_phase("2_nightly_fix_candidate.py", ["--project", pname, "--run-id", run_id])
        if rc2 != 0:
            print(f"[{pname}] Phase 2 exited with code {rc2}.")
            failed_projects.append((pname, "phase2", rc2))

    print(f"\n--- Generating Morning Summary ---")
    rc3 = run_phase("3_morning_summary.py", ["--run-id", run_id])
    elapsed = fmt_elapsed(time.time() - start_time)

    if rc3 != 0:
        log_path = os.path.join(".nightly_agent", "nightly.log")
        print(f"요약 생성 실패 (exit {rc3}). 로그: {os.path.abspath(log_path)}")

    if failed_projects:
        print(f"\nWARNING: {len(failed_projects)} phase(s) reported non-zero exit:")
        for pname, phase, rc in failed_projects:
            print(f"  {pname} / {phase} => exit {rc}")

    print(f"All projects processed. Elapsed: {elapsed}")

if __name__ == "__main__":
    main()
