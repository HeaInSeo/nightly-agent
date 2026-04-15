import os
import sys
import requests
from agent_core import AgentState, run_cmd

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "gemma4:26b"

def is_diff_too_large(diff_text):
    lines = diff_text.splitlines()
    if len(lines) > 2000:
        return True
    return False

def main():
    agent = AgentState()
    if not agent.acquire_lock():
        print("Another process is currently running or locked this run. Exiting idly.")
        sys.exit(0)

    try:
        state = agent.load_state()
        
        # 멱등성 검사
        if state.get("status_p1_review") in ["success", "skipped", "failed"]:
            print(f"P1 Review already in state: {state.get('status_p1_review')}. Exiting.")
            return

        state["status_p1_review"] = "running"
        agent.save_state(state)

        # Diff 수집
        diff_text, _, _ = run_cmd(f"git diff {state.get('target_commit')}~1 {state.get('target_commit')}")
        if not diff_text:
            state["status_p1_review"] = "skipped"
            state["reason"] = "No diff found."
            agent.save_state(state)
            return

        # 방어막: Diff 크기 제한
        if is_diff_too_large(diff_text):
            state["status_p1_review"] = "skipped"
            state["reason"] = "Diff too large (>2000 lines)."
            agent.save_state(state)
            return

        # LLM 리뷰 요청 시뮬레이션 및 마크다운 리포트 생성
        prompt = f"Analyze this code diff and provide a markdown review:\n\n{diff_text}"
        
        # 실제 환경 시 주석 해제 (메모리 증설 후)
        # response = requests.post(OLLAMA_URL, json={"model": MODEL_NAME, "prompt": prompt, "stream": False}).json()
        # review_content = response.get('response', 'Review failed.')
        
        review_content = "# Nightly Code Review Report\n\n(AI Review Pending Real Call)\n\nFound issues: \n1. [Issue A] Potential logical error."

        report_path = os.path.join(agent.get_run_dir(), "review_report.md")
        with open(report_path, "w") as f:
            f.write(review_content)

        state["status_p1_review"] = "success"
        state["issues_found"] = 1
        agent.save_state(state)
        print("P1 Review Complete.")

    except Exception as e:
        state["status_p1_review"] = "failed"
        state["error"] = str(e)
        agent.save_state(state)
    finally:
        agent.release_lock()

if __name__ == "__main__":
    main()
