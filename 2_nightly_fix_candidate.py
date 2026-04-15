import os
import sys
import shutil
from agent_core import AgentState, run_cmd

WORKTREE_PATH = ".nightly_agent/sandbox_worktree"

def cleanup_worktree():
    print("Executing trap cleanup: Removing sandbox worktree...")
    if os.path.exists(WORKTREE_PATH):
        run_cmd(f"git worktree remove --force {WORKTREE_PATH}")
    run_cmd("git branch -D auto-fix-tmp")

def main():
    agent = AgentState()
    if not agent.acquire_lock():
        sys.exit(0)

    try:
        state = agent.load_state()
        if state.get("status_p1_review") != "success":
            state["status_p2_fix"] = "skipped"
            agent.save_state(state)
            return
            
        if state.get("status_p2_fix") in ["success", "max_retries_reached", "skipped", "failed"]:
            return

        state["status_p2_fix"] = "running"
        agent.save_state(state)

        # 1. 샌드박스 생성
        cleanup_worktree() # 방어적 사전 정리
        out, err, code = run_cmd(f"git worktree add {WORKTREE_PATH} -b auto-fix-tmp {state.get('target_commit')}")
        if code != 0:
            raise Exception(f"Failed to create worktree: {err}")

        # 2. 패치 시도 루프 (최대 3회)
        MAX_RETRIES = 3
        success = False

        for attempt in range(1, MAX_RETRIES + 1):
            # LLM API를 통해 패치 생성 시뮬레이션
            candidate_patch = f"--- a/dummy.go\n+++ b/dummy.go\n@@ -1,1 +1,2 @@\n-old\n+new\n"
            
            patch_path = os.path.join(agent.get_run_dir(), f"candidate_{attempt}.patch")
            with open(patch_path, "w") as f:
                f.write(candidate_patch)
                
            # 패치 적용 전 Check (구조 검증)
            out, err, code = run_cmd(f"git apply --check ../../{patch_path}", cwd=WORKTREE_PATH)
            if code == 0:
                # 검증 성공 시 패치 적용
                run_cmd(f"git apply ../../{patch_path}", cwd=WORKTREE_PATH)
                # Lint / Test
                test_out, test_err, test_code = run_cmd("go test ./...", cwd=WORKTREE_PATH)
                if test_code == 0:
                    success = True
                    break
            
            print(f"Attempt {attempt} failed, throwing back to LLM...")

        if success:
            state["status_p2_fix"] = "success"
        else:
            state["status_p2_fix"] = "max_retries_reached"

        agent.save_state(state)
        print("P2 Fix Complete.")

    except Exception as e:
        state["status_p2_fix"] = "failed"
        state["error"] = str(e)
        agent.save_state(state)
    finally:
        # 무조건 실행되는 Cleanup Trap
        cleanup_worktree()
        agent.release_lock()

if __name__ == "__main__":
    main()
