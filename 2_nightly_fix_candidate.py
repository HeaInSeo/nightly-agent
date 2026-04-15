import os
import sys
import json
import requests
from agent_core import AgentState, run_cmd

WORKTREE_PATH = os.path.abspath(".nightly_agent/sandbox_worktree")

def ask_ollama(prompt, conf):
    url = conf.get("ollama_url", "http://localhost:11434/api/generate")
    model = conf.get("model_name", "gemma4:26b")
    try:
        response = requests.post(url, json={"model": model, "prompt": prompt, "stream": False}, timeout=600)
        response.raise_for_status()
        return response.json().get("response", "")
    except Exception as e:
        raise RuntimeError(f"Ollama API Call Failed: {e}")

def cleanup_worktree(branch_name):
    if os.path.exists(WORKTREE_PATH):
        run_cmd(f"git worktree remove --force {WORKTREE_PATH}")
    run_cmd(f"git branch -D {branch_name}") 

def extract_patch(ai_response):
    lines = ai_response.splitlines()
    patch_lines = []
    in_patch = False
    for line in lines:
        if line.startswith("```diff") or line.startswith("```patch"):
            in_patch = True
            continue
        elif line.startswith("```") and in_patch:
            break
        
        if in_patch or line.startswith("--- a/") or line.startswith("+++ b/"):
            in_patch = True
            patch_lines.append(line)
    
    if not patch_lines:
        return ai_response 
    return "\n".join(patch_lines) + "\n"

def main():
    agent = AgentState()
    if not agent.acquire_lock():
        sys.exit(0)

    # 파생 브랜치명 고유화 방어
    branch_name = f"auto-fix-{agent.run_id}"

    try:
        config = agent.config
        state = agent.load_state()
        
        if state.get("status_p1_review") != "success" or state.get("status_p2_fix") not in ["pending"]:
            return

        state["started_p2_at"] = os.popen('date -Iseconds').read().strip()
        state["status_p2_fix"] = "running"
        agent.save_state(state)
        
        issues = []
        issues_file = state.get("issues_file")
        if issues_file and os.path.exists(issues_file):
            with open(issues_file, "r") as f:
                issues = json.load(f)
                
        if not issues:
            state["status_p2_fix"] = "skipped"
            agent.save_state(state)
            return

        cleanup_worktree(branch_name)
        out, err, code = run_cmd(f"git worktree add {WORKTREE_PATH} -b {branch_name} {state.get('target_commit')}")
        if code != 0:
            raise Exception(f"Failed to create worktree: {err}")

        max_retries = config.get("max_retries", 3)
        test_cmd = config.get("test_command", "go test ./...")
        success = False
        feedback_log = "Initial attempt"
        target_issue = issues[0]
        final_best_patch = None

        for attempt in range(1, max_retries + 1):
            state["attempt_count"] = attempt
            agent.save_state(state)
            
            prompt = f"Fix the following issue by generating a unified git patch.\n\nIssue:\n{json.dumps(target_issue)}\n\nFeedback from previous attempt:\n{feedback_log}\n\nPlease output ONLY the unified patch inside a ```diff block."
            
            ai_response = ask_ollama(prompt, config)
            candidate_patch = extract_patch(ai_response)
            
            patch_path = os.path.join(agent.get_run_dir(), f"candidate_{attempt}.patch")
            with open(patch_path, "w") as f:
                f.write(candidate_patch)
                
            out, err, code = run_cmd(f"git apply --check {patch_path}", cwd=WORKTREE_PATH)
            if code == 0:
                run_cmd(f"git apply {patch_path}", cwd=WORKTREE_PATH)
                test_out, test_err, test_code = run_cmd(test_cmd, cwd=WORKTREE_PATH)
                
                with open(os.path.join(agent.get_run_dir(), f"candidate_{attempt}_test.log"), "w") as tf:
                    tf.write(test_out + "\n" + test_err)
                    
                if test_code == 0:
                    success = True
                    final_best_patch = patch_path
                    break
                else:
                    feedback_log = f"Patch applied but tests failed. Test Error:\n{test_err}\nPlease rewrite the patch."
                    run_cmd(f"git restore .", cwd=WORKTREE_PATH) 
            else:
                feedback_log = f"Patch rejected by git apply --check. Error:\n{err}\nPlease provide a correctly formatted unified patch."

        state["finished_p2_at"] = os.popen('date -Iseconds').read().strip()
        state["status_p2_fix"] = "success" if success else "max_retries_reached"
        if final_best_patch:
            state["best_patch_path"] = final_best_patch
            
        agent.save_state(state)

    except Exception as e:
        state["status_p2_fix"] = "failed"
        state["error_message"] = str(e)
        agent.save_state(state)
    finally:
        cleanup_worktree(branch_name)
        agent.release_lock()

if __name__ == "__main__":
    main()
