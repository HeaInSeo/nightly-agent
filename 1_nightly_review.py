import os
import sys
import json
import requests
from agent_core import AgentState, run_cmd

def main():
    agent = AgentState()
    if not agent.acquire_lock():
        print("Run locked or another process is busy. Exiting.")
        sys.exit(0)

    try:
        config = agent.config
        state = agent.load_state()
        
        if state.get("status_p1_review") in ["success", "skipped", "failed"]:
            print("P1 Review already processed. Exiting.")
            return

        state["started_p1_at"] = os.popen('date -Iseconds').read().strip()
        state["status_p1_review"] = "running"
        agent.save_state(state)

        # Base branch 명시적 Diff 확인
        base_branch = config.get("base_branch", "master")
        diff_text, err, code = run_cmd(f"git diff {base_branch}...{state.get('target_commit')}")
        
        # fallback (리모트 브랜치 추적이 없거나 최상단 커밋일 때)
        if code != 0 or not diff_text:
            diff_text, _, _ = run_cmd("git show HEAD")

        if not diff_text.strip():
            state["status_p1_review"] = "skipped"
            state["error_message"] = "No structural diff found against base."
            agent.save_state(state)
            return

        lines = diff_text.splitlines()
        if len(lines) > config.get("max_diff_lines", 2000):
            state["status_p1_review"] = "skipped"
            state["error_message"] = "Diff too large, degrading to review-only mode (skip fixes)."
            agent.save_state(state)
            return

        # LLM 실제 호출 연동 준비 
        # API Response 시뮬레이션
        review_content = "# Nightly Code Review Report\n\nAnalyzed latest commits.\n\nIssues found:\n1. Error handling bypass detected."
        structured_issues = [
            {
                "id": "issue-1",
                "title": "Unchecked Error",
                "severity": "medium",
                "target_files": ["example.go"],
                "suggested_action": "Add explicit error check before returning."
            }
        ]

        # Artifact 저장
        report_path = os.path.join(agent.get_run_dir(), "review_report.md")
        with open(report_path, "w") as f:
            f.write(review_content)
            
        issues_path = os.path.join(agent.get_run_dir(), "issues.json")
        with open(issues_path, "w") as f:
            json.dump(structured_issues, f, indent=4)

        state["review_report_path"] = report_path
        state["issues_file"] = issues_path
        state["issues_count"] = len(structured_issues)
        state["finished_p1_at"] = os.popen('date -Iseconds').read().strip()
        state["status_p1_review"] = "success"
        agent.save_state(state)

    except Exception as e:
        state["status_p1_review"] = "failed"
        state["error_message"] = str(e)
        agent.save_state(state)
    finally:
        agent.release_lock()

if __name__ == "__main__":
    main()
