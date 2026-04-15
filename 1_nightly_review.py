import os
import sys
import json
import requests
import re
from agent_core import AgentState, run_cmd

def ask_ollama(prompt, conf):
    url = conf.get("ollama_url", "http://localhost:11434/api/generate")
    model = conf.get("model_name", "gemma4:26b")
    try:
        response = requests.post(url, json={"model": model, "prompt": prompt, "stream": False}, timeout=600)
        response.raise_for_status()
        return response.json().get("response", "")
    except Exception as e:
        raise RuntimeError(f"Ollama API Call Failed: {e}")

def main():
    agent = AgentState()
    if not agent.acquire_lock():
        sys.exit(0)

    try:
        config = agent.config
        state = agent.load_state()
        
        if state.get("status_p1_review") in ["success", "skipped", "failed"]:
            return

        state["started_p1_at"] = os.popen('date -Iseconds').read().strip()
        state["status_p1_review"] = "running"
        agent.save_state(state)

        base_branch = config.get("base_branch", "master")
        target_commit = state.get("target_commit")
        
        merge_base_out, err, code = run_cmd(f"git merge-base {base_branch} {target_commit}")
        if code != 0:
            state["status_p1_review"] = "failed"
            state["error_message"] = f"Diff merge base failed. Make sure {base_branch} exists locally."
            agent.save_state(state)
            return
            
        merge_base = merge_base_out.strip()
        state["base_branch"] = base_branch
        state["merge_base"] = merge_base
        state["diff_strategy"] = f"git diff {base_branch}...{target_commit}"
        
        diff_text, err, code = run_cmd(state["diff_strategy"])

        if code != 0 or not diff_text.strip():
            state["status_p1_review"] = "skipped"
            state["error_message"] = "No structural diff found against base. Skipped review."
            agent.save_state(state)
            return

        if len(diff_text.splitlines()) > config.get("max_diff_lines", 2000):
            state["status_p1_review"] = "skipped"
            state["error_message"] = "Diff too large, degrading to review-only mode (skip fixes)."
            agent.save_state(state)
            return

        report_prompt = f"You are a Senior Engineer. Review this code diff and output a markdown report detailing logical bugs, security flaws, and optimization points.\n\n[Diff]\n{diff_text}"
        review_content = ask_ollama(report_prompt, config)

        json_prompt = f"Based on the following diff, output ONLY a valid JSON array of issues (and absolutely no other text, no markdown backticks). [{{\"id\":\"issue-1\", \"title\":\"short title\", \"severity\":\"high/medium/low\", \"target_files\":[\"file.ext\"], \"suggested_action\":\"fix description\"}}]. If no issues, output []. \n[Diff]\n{diff_text}"
        json_res = ask_ollama(json_prompt, config)
        
        try:
            json_res = json_res.replace("```json", "").replace("```", "").strip()
            structured_issues = json.loads(json_res)
        except json.JSONDecodeError:
            structured_issues = []

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
