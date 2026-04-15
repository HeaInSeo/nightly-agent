import os
import sys
import json
from agent_core import AgentState, run_cmd

WORKTREE_PATH = os.path.abspath(".nightly_agent/sandbox_worktree")

def cleanup_worktree():
    if os.path.exists(WORKTREE_PATH):
        run_cmd(f"git worktree remove --force {WORKTREE_PATH}")
    run_cmd("git branch -D auto-fix-tmp") # 무시될 수 있음

def main():
    agent = AgentState()
    if not agent.acquire_lock():
        sys.exit(0)

    try:
        config = agent.config
        state = agent.load_state()
        
        if state.get("status_p1_review") != "success":
            state["status_p2_fix"] = "skipped"
            agent.save_state(state)
            return
            
        if state.get("status_p2_fix") not in ["pending"]:
            return

        state["started_p2_at"] = os.popen('date -Iseconds').read().strip()
        state["status_p2_fix"] = "running"
        agent.save_state(state)
        
        # issues.json 읽기
        issues = []
        issues_file = state.get("issues_file")
        if issues_file and os.path.exists(issues_file):
            with open(issues_file, "r") as f:
                issues = json.load(f)
                
        if not issues:
            state["status_p2_fix"] = "skipped"
            state["error_message"] = "No issues found."
            agent.save_state(state)
            return

        cleanup_worktree()
        out, err, code = run_cmd(f"git worktree add {WORKTREE_PATH} -b auto-fix-tmp {state.get('target_commit')}")
        if code != 0:
            raise Exception(f"Failed to create worktree: {err}")

        max_retries = config.get("max_retries", 3)
        test_command = config.get("test_command", "go test ./...")
        success = False

        for attempt in range(1, max_retries + 1):
            state["attempt_count"] = attempt
            agent.save_state(state)
            
            # API Simulation: 생성된 패치(절대경로 적용)
            candidate_patch = f"--- a/README.md\n+++ b/README.md\n@@ -1,1 +1,2 @@\n-# nightly-agent\n+# nightly-agent\n+Fix attempt {attempt}\n"
            
            # 절대 경로로 Patch 저장
            patch_path = os.path.join(agent.get_run_dir(), f"candidate.patch")
            with open(patch_path, "w") as f:
                f.write(candidate_patch)
                
            # 절대 경로로 apply check
            out, err, code = run_cmd(f"git apply --check {patch_path}", cwd=WORKTREE_PATH)
            if code == 0:
                run_cmd(f"git apply {patch_path}", cwd=WORKTREE_PATH)
                test_out, test_err, test_code = run_cmd(test_command, cwd=WORKTREE_PATH)
                
                # 테스트 로그도 저장
                with open(os.path.join(agent.get_run_dir(), "candidate_test.log"), "w") as tf:
                    tf.write(test_out + "\n" + test_err)
                    
                if test_code == 0:
                    success = True
                    break
        
        state["finished_p2_at"] = os.popen('date -Iseconds').read().strip()
        state["status_p2_fix"] = "success" if success else "max_retries_reached"
        agent.save_state(state)

    except Exception as e:
        state["status_p2_fix"] = "failed"
        state["error_message"] = str(e)
        agent.save_state(state)
    finally:
        cleanup_worktree()
        agent.release_lock()

if __name__ == "__main__":
    main()
